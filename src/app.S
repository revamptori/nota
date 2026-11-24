/*
 * app.S - application state and the note model.
 *
 * This is the layer between the editor/store primitives and the screens: it
 * owns the note array, the currently edited document, and the small amount of
 * session state (which note is open, what the list is scrolled to). Keeping it
 * separate from the screens means the painters are readable as pure functions
 * of this struct, and it means the note model can be tested without GL.
 */
#include "nota.h"

    .equ SC_SCR,      0x00   /* long: 0 list, 1 editor                     */
    .equ SC_SEL,      0x08   /* long: index of the open note, or -1        */
    .equ SC_DIRTY,    0x10   /* long: unsaved changes                      */
    .equ SC_NEXT_ID,  0x18   /* long: next note id                         */
    .equ SC_SIZE,     0x20

    .equ ED_NOTE,     0x00   /* note* being edited                         */
    .equ ED_SIZE,     0x08

/* ========================================================================== *
 * Note store: a flat array plus a count.
 *
 * A notes app this size does not need an index: MAX_NOTES is 512 and every
 * query is a linear pass over 40-byte structs. When it does need one, the
 * format on disk (length-prefixed records) already supports skipping.
 * ========================================================================== */
    .section .bss
    .p2align 4
    .globl nota_store
    .hidden nota_store
    .type  nota_store, %object
nota_store:
    .space 0x40
    .size nota_store, . - nota_store

    .p2align 4
    .globl nota_doc
    .hidden nota_doc
    .type  nota_doc, %object
nota_doc:
    .space EDIT_DOC_SIZE
    .size nota_doc, . - nota_doc

    .p2align 4
    .globl nota_edit
    .hidden nota_edit
    .type  nota_edit, %object
nota_edit:
    .space ED_SIZE
    .size nota_edit, . - nota_edit

    .p2align 4
    .globl nota_app
    .hidden nota_app
    .type  nota_app, %object
nota_app:
    .space SC_SIZE
    .size nota_app, . - nota_app

    /* MAX_NOTES * NOTE_SIZE */
    .p2align 4
    .globl nota_notes
    .hidden nota_notes
    .type  nota_notes, %object
nota_notes:
    .space MAX_NOTES * NOTE_SIZE
    .size nota_notes, . - nota_notes

    /* per-note text arena: titles, bodies and tag id arrays */
    .p2align 4
    .globl nota_arena
    .hidden nota_arena
    .type  nota_arena, %object
nota_arena:
    .space MAX_NOTES * MAX_NOTE_BYTES
    .size nota_arena, . - nota_arena

    .globl nota_arena_used
    .hidden nota_arena_used
    .type  nota_arena_used, %object
nota_arena_used:
    .space 8

    /* the edited document's buffer, separate from the note it came from so
       undo can be discarded without touching the saved text */
    .p2align 4
    .globl nota_editbuf
    .hidden nota_editbuf
    .type  nota_editbuf, %object
nota_editbuf:
    .space MAX_NOTE_BYTES + 1
    .size nota_editbuf, . - nota_editbuf

    /* scratch for integer formatting */
    .globl nota_num
    .hidden nota_num
    .type  nota_num, %object
nota_num:
    .space 32
    .size nota_num, . - nota_num

/* store header offsets */
    .equ ST_NOTES,    0x00
    .equ ST_COUNT,    0x08
    .equ ST_ARENA,    0x10
    .equ ST_USED,     0x18
    .equ ST_DIRTY,    0x20

/* note array lives at nota_store + ST_NOTES */
    .equ NOTES_PTR,   (nota_notes - nota_store)
    .equ ARENA_OFF,   (nota_arena - nota_store)

    .text

/* ========================================================================= *
 * long nota_notes_init(void)
 * Point the store at its arrays. Call once, before anything else.
 * ========================================================================= */
    .globl nota_notes_init
    .type  nota_notes_init, %function
nota_notes_init:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    adrp   x10, nota_notes
    add    x10, x10, :lo12:nota_notes
    str    x10, [x9, #ST_NOTES]
    str    xzr, [x9, #ST_COUNT]
    adrp   x11, nota_arena
    add    x11, x11, :lo12:nota_arena
    str    x11, [x9, #ST_ARENA]
    str    xzr, [x9, #ST_USED]
    str    xzr, [x9, #ST_DIRTY]

    /* session state */
    adrp   x9, nota_app
    add    x9, x9, :lo12:nota_app
    str    xzr, [x9, #SC_SCR]
    mov    x10, #-1
    str    x10, [x9, #SC_SEL]
    str    xzr, [x9, #SC_DIRTY]
    mov    x10, #1
    str    x10, [x9, #SC_NEXT_ID]

    /* the editor's document needs a buffer before anything can type */
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    adrp   x1, nota_editbuf
    add    x1, x1, :lo12:nota_editbuf
    mov    x2, #MAX_NOTE_BYTES
    bl     nota_doc_init
    mov    x0, #0
    ldp    x29, x30, [sp], #32
    ret
    .size nota_notes_init, . - nota_notes_init

/* ========================================================================= *
 * note *nota_note_new(void) - append an empty note and return it, or NULL.
 * ========================================================================= */
    .globl nota_note_new
    .type  nota_note_new, %function
nota_note_new:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    ldr    x10, [x9, #ST_COUNT]
    mov    w11, #MAX_NOTES
    cmp    x10, x11
    b.hs   .Lnn_null

    /* slot = notes + count * NOTE_SIZE */
    ldr    x12, [x9, #ST_NOTES]
    mov    x13, #NOTE_SIZE
    madd   x19, x10, x13, x12   /* x19: the new note, live across bl */
    stp    xzr, xzr, [x19]
    stp    xzr, xzr, [x19, #16]
    stp    xzr, xzr, [x19, #32]
    stp    xzr, xzr, [x19, #48]

    /* Every note owns one fixed-size slice of the arena, indexed by its
       position in the array. Title and body share the slice because a note's
       title *is* its first line - storing them separately is how the two
       regions ended up overlapping and clipping each other's text. */
    ldr    x14, [x9, #ST_ARENA]
    mov    w15, #MAX_NOTE_BYTES
    madd   x14, x10, x15, x14
    stp    x14, x14, [x19, #NOTE_TITLE_PTR]   /* title ptr, body ptr */
    str    x14, [x19, #NOTE_TAG_PTR]
    str    wzr, [x19, #NOTE_TITLE_LEN]
    str    wzr, [x19, #NOTE_BODY_LEN]
    strb   wzr, [x14]

    adrp   x15, nota_app
    add    x15, x15, :lo12:nota_app
    ldr    x16, [x15, #SC_NEXT_ID]
    str    w16, [x19, #NOTE_ID]
    add    x16, x16, #1
    str    x16, [x15, #SC_NEXT_ID]

    bl     nota_now_ms
    str    x0, [x19, #NOTE_CREATED]
    str    x0, [x19, #NOTE_UPDATED]

    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    ldr    x10, [x9, #ST_COUNT]
    add    x10, x10, #1
    str    x10, [x9, #ST_COUNT]
    mov    x0, x19
    b      .Lnn_out
.Lnn_null:
    mov    x0, #0
.Lnn_out:
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_note_new, . - nota_note_new

/* ========================================================================= *
 * void nota_note_remove(long index) - delete a note, sliding the rest down.
 * ========================================================================= */
    .globl nota_note_remove
    .type  nota_note_remove, %function
nota_note_remove:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    ldr    x10, [x9, #ST_COUNT]
    cmp    x0, x10
    b.hs   .Lnr_out
    ldr    x11, [x9, #ST_NOTES]
    mov    w12, #NOTE_SIZE
    madd   x19, x0, x12, x11    /* dst: the removed slot  */
    add    x20, x19, x12        /* src: the next note     */
    sub    x21, x10, x0
    sub    x21, x21, #1         /* notes after it         */
    cbz    x21, .Lnr_dec
    mov    x0, x19
    mov    x1, x20
    mov    x2, #NOTE_SIZE
    mul    x2, x21, x2
    bl     nota_memmove
.Lnr_dec:
    sub    x10, x10, #1
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    str    x10, [x9, #ST_COUNT]
    mov    x10, #1
    str    x10, [x9, #ST_DIRTY]
.Lnr_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_note_remove, . - nota_note_remove

/* ========================================================================= *
 * note *nota_note_at(long index) - bounds-checked access, NULL if out of range.
 * ========================================================================= */
    .globl nota_note_at
    .type  nota_note_at, %function
nota_note_at:
    tbnz   x0, #63, .Lna_null
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    ldr    x10, [x9, #ST_COUNT]
    cmp    x0, x10
    b.hs   .Lna_null
    ldr    x11, [x9, #ST_NOTES]
    mov    x12, #NOTE_SIZE
    madd   x0, x0, x12, x11
    ret
.Lna_null:
    mov    x0, #0
    ret
    .size nota_note_at, . - nota_note_at

/* ========================================================================= *
 * long nota_note_count(void)
 * ========================================================================= */
    .globl nota_note_count
    .type  nota_note_count, %function
nota_note_count:
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    ldr    x0, [x9, #ST_COUNT]
    ret
    .size nota_note_count, . - nota_note_count

/* ========================================================================= *
 * long nota_note_open(long index) - load a note's body into the editor.
 * The title is copied through as the body: NOTA treats the first line as the
 * title, which is why the list shows title and first body line separately.
 * ========================================================================= */
    .globl nota_note_open
    .type  nota_note_open, %function
nota_note_open:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    bl     nota_note_at
    cbz    x0, .Lno_fail
    mov    x19, x0

    /* the editor keeps the note it is editing so the title bar can show it */
    adrp   x9, nota_edit
    add    x9, x9, :lo12:nota_edit
    str    x19, [x9, #ED_NOTE]

    adrp   x20, nota_doc
    add    x20, x20, :lo12:nota_doc
    ldr    x1, [x19, #NOTE_BODY_PTR]
    ldr    w2, [x19, #NOTE_BODY_LEN]
    mov    x0, x20
    bl     nota_doc_set_text

    /* session: editing, clean */
    adrp   x9, nota_app
    add    x9, x9, :lo12:nota_app
    str    x19, [x9, #SC_SEL]
    mov    x10, #1
    str    x10, [x9, #SC_SCR]
    str    xzr, [x9, #SC_DIRTY]
    mov    x0, #0
    b      .Lno_out
.Lno_fail:
    mov    x0, #-1
.Lno_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_note_open, . - nota_note_open

/* ========================================================================= *
 * long nota_note_commit(void) - write the editor's text back into the note.
 * The body is stored in the arena, so committing repeatedly would leak; the
 * note keeps the region it was given when its text grew, and a note that only
 * ever shrinks reuses that region.
 * ========================================================================= */
    .globl nota_note_commit
    .type  nota_note_commit, %function
nota_note_commit:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    adrp   x9, nota_edit
    add    x9, x9, :lo12:nota_edit
    ldr    x19, [x9]
    cbz    x19, .Lnc_fail
    adrp   x20, nota_doc
    add    x20, x20, :lo12:nota_doc
    ldr    x21, [x20, #DOC_LEN]
    mov    w9, #MAX_NOTE_BYTES
    cmp    x21, x9
    b.hi   .Lnc_fail

    /* The note's body region is a fixed slice, so a commit is always a plain
       overwrite; nothing to allocate and nothing to leak. */
    ldr    x0, [x19, #NOTE_BODY_PTR]
    ldr    x1, [x20, #DOC_BUF]
    mov    x2, x21
    bl     nota_memcpy
    str    w21, [x19, #NOTE_BODY_LEN]
    mov    x0, x19
    /* terminator, written after the copy since memcpy returns the end */
    ldr    x10, [x19, #NOTE_BODY_PTR]
    strb   wzr, [x10, x21]

    /* title = first line of the body, capped for display */
    mov    x0, x10
    mov    x1, x21
    bl     nota_first_line_len
    mov    w9, #MAX_TITLE_LEN
    cmp    x0, x9
    b.ls   1f
    mov    x0, x9
1:  str    w0, [x19, #NOTE_TITLE_LEN]

    bl     nota_now_ms
    str    x0, [x19, #NOTE_UPDATED]
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    mov    x10, #1
    str    x10, [x9, #ST_DIRTY]
    adrp   x9, nota_app
    add    x9, x9, :lo12:nota_app
    str    xzr, [x9, #SC_DIRTY]
    mov    x0, #0
    b      .Lnc_out
.Lnc_fail:
    mov    x0, #-1
.Lnc_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_note_commit, . - nota_note_commit

/* ========================================================================= *
 * long nota_first_line_len(const char *s, long len) - bytes before '\n'.
 * ========================================================================= */
    .globl nota_first_line_len
    .type  nota_first_line_len, %function
nota_first_line_len:
    mov    x9, #0
.Lfl_loop:
    cmp    x9, x1
    b.hs   .Lfl_done
    ldrb   w10, [x0, x9]
    cmp    w10, #10
    b.eq   .Lfl_done
    add    x9, x9, #1
    b      .Lfl_loop
.Lfl_done:
    mov    x0, x9
    ret
    .size nota_first_line_len, . - nota_first_line_len

/* ========================================================================= *
 * void nota_note_close(void) - commit and return to the list.
 * ========================================================================= */
    .globl nota_note_close
    .type  nota_note_close, %function
nota_note_close:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    bl     nota_note_commit
    adrp   x9, nota_edit
    add    x9, x9, :lo12:nota_edit
    str    xzr, [x9, #ED_NOTE]
    adrp   x9, nota_app
    add    x9, x9, :lo12:nota_app
    str    xzr, [x9, #SC_SCR]
    mov    x10, #-1
    str    x10, [x9, #SC_SEL]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_note_close, . - nota_note_close

    .section .note.GNU-stack,"",%progbits
