/*
 * edit.S - the text editor core.
 *
 * The part of NOTA that must be exactly right: a document buffer, a caret and
 * selection measured in code points, an undo history of reversible edits, and
 * the commands a notes editor needs. No Android dependency, so every rule is
 * unit-tested on the host against the real machine code.
 *
 * ONE primitive does buffer surgery: nota_doc_replace(d, start, end, text,
 * len). Insert, typing, paste, delete, backspace, select-replace and undo/redo
 * all funnel through it, so capacity limits, code point accounting, dirty
 * tracking and caret clamping are implemented exactly once.
 *
 * Undo entries hold only the reversible part of an edit: offset, inserted
 * bytes, removed bytes. Undo removes what was inserted and restores what was
 * removed; redo does the reverse. Payloads live in a caller-provided arena
 * with a bump allocator; when it is exhausted the history is reset rather than
 * silently corrupting a later undo.
 *
 * Typing bursts: consecutive single code point insertions at the caret merge
 * into one undo step (DOC_BURST records where the live burst ends). Anything
 * else - a selection, a delete, a cursor jump, paste - ends the burst.
 */
#include "nota.h"

/* document layout (EDIT_DOC_SIZE) */
#define D_BUF        0x00
#define D_CAP        0x08
#define D_LEN        0x10
#define D_CPS        0x18
#define D_CURSOR     0x20
#define D_ANCH       0x28
#define D_DIRTY      0x30
#define D_SELECTING  0x38
#define D_BURST      0x40
#define D_UNC        0x48
#define D_UNP        0x50
#define D_UENT       0x58
#define D_UNMAX      0x60
#define D_UARENA     0x68
#define D_UACAP      0x70
#define D_UAN        0x78
#define D_GOALCOL    0x80
#define D_GOALSET    0x88     /* cursor offset the goal column was measured at */

/* undo entry layout */
#define U_OFF        0x00
#define U_INSLEN     0x08
#define U_DELLEN     0x10
#define U_INSPTR     0x18
#define U_DELPTR     0x20
#define U_SIZE       0x28

    .text

/* ========================================================================= *
 * Caret, selection and small text helpers
 * ========================================================================= */

/* void nota_clamp_cursor(const char *buf, long len, long *off) */
    .globl nota_clamp_cursor
    .type  nota_clamp_cursor, %function
nota_clamp_cursor:
    ldr    x3, [x2]
    tbnz   x3, #63, .Lcc_zero
    cmp    x3, x1
    b.ls   .Lcc_scan
    mov    x3, x1
.Lcc_scan:
    cbz    x3, .Lcc_store
    ldrb   w4, [x0, x3]
    and    w4, w4, #0xC0
    cmp    w4, #0x80              /* continuation byte: step back into it */
    b.ne   .Lcc_store
    sub    x3, x3, #1
    b      .Lcc_scan
.Lcc_zero:
    mov    x3, #0
.Lcc_store:
    str    x3, [x2]
    ret
    .size nota_clamp_cursor, . - nota_clamp_cursor

/* long nota_doc_sel_start(edit_doc *d) */
    .globl nota_doc_sel_start
    .type  nota_doc_sel_start, %function
nota_doc_sel_start:
    ldr    x1, [x0, #D_CURSOR]
    ldr    x2, [x0, #D_ANCH]
    cmp    x1, x2
    csel   x0, x1, x2, lo
    ret
    .size nota_doc_sel_start, . - nota_doc_sel_start

/* long nota_doc_sel_end(edit_doc *d) */
    .globl nota_doc_sel_end
    .type  nota_doc_sel_end, %function
nota_doc_sel_end:
    ldr    x1, [x0, #D_CURSOR]
    ldr    x2, [x0, #D_ANCH]
    cmp    x1, x2
    csel   x0, x1, x2, hs
    ret
    .size nota_doc_sel_end, . - nota_doc_sel_end

/* long nota_doc_has_selection(edit_doc *d) */
    .globl nota_doc_has_selection
    .type  nota_doc_has_selection, %function
nota_doc_has_selection:
    ldr    x1, [x0, #D_CURSOR]
    ldr    x2, [x0, #D_ANCH]
    cmp    x1, x2
    cset   x0, ne
    ret
    .size nota_doc_has_selection, . - nota_doc_has_selection

/* void nota_doc_clear_selection(edit_doc *d) */
    .globl nota_doc_clear_selection
    .type  nota_doc_clear_selection, %function
nota_doc_clear_selection:
    ldr    x1, [x0, #D_CURSOR]
    str    x1, [x0, #D_ANCH]
    str    xzr, [x0, #D_SELECTING]
    ret
    .size nota_doc_clear_selection, . - nota_doc_clear_selection

/* long nota_utf8_trim(const char *s, long len)
 * Largest length <= len that does not end inside a multi-byte sequence, so a
 * file truncated by a crash loads with only its partial tail dropped.
 */
    .globl nota_utf8_trim
    .type  nota_utf8_trim, %function
nota_utf8_trim:
    mov    x9, x1
.Lut_loop:
    cbz    x9, .Lut_done
    ldrb   w10, [x0, x9]           /* byte at the cut (NUL when x9 == len) */
    and    w11, w10, #0xC0
    cmp    w11, #0x80              /* the cut lands inside a sequence */
    b.ne   .Lut_lead
    sub    x9, x9, #1
    b      .Lut_loop
.Lut_lead:
    sub    x12, x9, #1             /* candidate lead byte */
    ldrb   w13, [x0, x12]
    and    w14, w13, #0x80
    cbz    w14, .Lut_done          /* ASCII before the cut: legal */
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x9, x12, [sp, #16]
    mov    w0, w13
    bl     nota_utf8_seqlen
    ldp    x9, x12, [sp, #16]
    ldp    x29, x30, [sp], #32
    sub    x15, x9, x12            /* sequence bytes actually present */
    cmp    x15, x0
    b.hs   .Lut_done               /* complete: the cut is legal */
    mov    x9, x12                 /* truncated: drop the whole sequence */
    b      .Lut_loop
.Lut_done:
    mov    x0, x9
    ret
    .size nota_utf8_trim, . - nota_utf8_trim

/* long nota_line_start(const char *buf, long off) */
    .globl nota_line_start
    .type  nota_line_start, %function
nota_line_start:
    mov    x9, x1
.Lls_loop:
    cbz    x9, .Lls_done
    sub    x9, x9, #1
    ldrb   w2, [x0, x9]
    cmp    w2, #'\n'
    b.ne   .Lls_loop
    add    x9, x9, #1
.Lls_done:
    mov    x0, x9
    ret
    .size nota_line_start, . - nota_line_start

/* long nota_line_end(const char *buf, long len, long off) */
    .globl nota_line_end
    .type  nota_line_end, %function
nota_line_end:
    mov    x9, x2
.Lle_loop:
    cmp    x9, x1
    b.hs   .Lle_done
    ldrb   w3, [x0, x9]
    cmp    w3, #'\n'
    b.eq   .Lle_done
    add    x9, x9, #1
    b      .Lle_loop
.Lle_done:
    mov    x0, x9
    ret
    .size nota_line_end, . - nota_line_end

/* long nota_line_of(const char *buf, long len, long off) - 0-based line index */
    .globl nota_line_of
    .type  nota_line_of, %function
nota_line_of:
    cmp    x2, x1
    csel   x2, x2, x1, lo
    mov    x9, #0
    mov    x10, #0
.Llo_loop:
    cmp    x10, x2
    b.hs   .Llo_done
    ldrb   w11, [x0, x10]
    cmp    w11, #'\n'
    b.ne   1f
    add    x9, x9, #1
1:  add    x10, x10, #1
    b      .Llo_loop
.Llo_done:
    mov    x0, x9
    ret
    .size nota_line_of, . - nota_line_of

/* long nota_line_count(const char *buf, long len) - at least 1 */
    .globl nota_line_count
    .type  nota_line_count, %function
nota_line_count:
    mov    x9, #1
    mov    x10, #0
.Llc_loop:
    cmp    x10, x1
    b.hs   .Llc_done
    ldrb   w11, [x0, x10]
    cmp    w11, #'\n'
    b.ne   1f
    add    x9, x9, #1
1:  add    x10, x10, #1
    b      .Llc_loop
.Llc_done:
    mov    x0, x9
    ret
    .size nota_line_count, . - nota_line_count

/* ========================================================================= *
 * Lifecycle
 * ========================================================================= */

/* void nota_doc_init(edit_doc *d, char *buf, long cap) */
    .globl nota_doc_init
    .type  nota_doc_init, %function
nota_doc_init:
    str    x1, [x0, #D_BUF]
    str    x2, [x0, #D_CAP]
    str    xzr, [x0, #D_LEN]
    str    xzr, [x0, #D_CPS]
    str    xzr, [x0, #D_CURSOR]
    str    xzr, [x0, #D_ANCH]
    str    xzr, [x0, #D_DIRTY]
    str    xzr, [x0, #D_SELECTING]
    mov    x3, #-1
    str    x3, [x0, #D_BURST]      /* no live typing burst */
    str    xzr, [x0, #D_GOALCOL]
    str    x3, [x0, #D_GOALSET]    /* no remembered column */
    str    xzr, [x0, #D_UNC]
    str    xzr, [x0, #D_UNP]
    str    xzr, [x0, #D_UENT]
    str    xzr, [x0, #D_UNMAX]
    str    xzr, [x0, #D_UARENA]
    str    xzr, [x0, #D_UACAP]
    str    xzr, [x0, #D_UAN]
    strb   wzr, [x1]
    ret
    .size nota_doc_init, . - nota_doc_init

/* void nota_doc_init_undo(edit_doc *d, void *entries, long max,
 *                         char *arena, long arena_cap)
 * The undo storage is caller-owned, so the size of the history is a startup
 * decision rather than a hidden allocation.
 */
    .globl nota_doc_init_undo
    .type  nota_doc_init_undo, %function
nota_doc_init_undo:
    str    x1, [x0, #D_UENT]
    str    x2, [x0, #D_UNMAX]
    str    x3, [x0, #D_UARENA]
    str    x4, [x0, #D_UACAP]
    str    xzr, [x0, #D_UNC]
    str    xzr, [x0, #D_UNP]
    str    xzr, [x0, #D_UAN]
    ret
    .size nota_doc_init_undo, . - nota_doc_init_undo

/* void nota_undo_clear(edit_doc *d) */
    .globl nota_undo_clear
    .type  nota_undo_clear, %function
nota_undo_clear:
    str    xzr, [x0, #D_UNC]
    str    xzr, [x0, #D_UNP]
    str    xzr, [x0, #D_UAN]
    mov    x1, #-1
    str    x1, [x0, #D_BURST]
    ret
    .size nota_undo_clear, . - nota_undo_clear

/* ========================================================================= *
 * Code point accounting
 * ========================================================================= */

/* void nota_doc_recount(edit_doc *d) */
    .globl nota_doc_recount
    .type  nota_doc_recount, %function
nota_doc_recount:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    str    x19, [sp, #16]
    mov    x19, x0
    ldr    x0, [x19, #D_BUF]
    ldr    x1, [x19, #D_LEN]
    bl     nota_utf8_len
    str    x0, [x19, #D_CPS]
    ldr    x19, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_doc_recount, . - nota_doc_recount

/* ========================================================================= *
 * Undo recording
 * ========================================================================= */

/* long nota_undo_push(edit_doc *d, long off, long inslen, long dellen,
 *                     const char *ins, const char *del)
 * Copies both payloads into the arena and appends an entry. Returns 1 on
 * success, 0 if the history had to be reset (in which case the edit still
 * happens - losing undo is acceptable, losing data is not).
 *
 * Merging: a pure insertion (dellen == 0) at exactly the end of the live burst
 * extends that entry instead of creating a new one.
 */
    .globl nota_undo_push
    .type  nota_undo_push, %function
nota_undo_push:
    stp    x29, x30, [sp, #-96]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    str    x27, [sp, #80]
    mov    x19, x0              /* doc   */
    mov    x20, x1              /* off   */
    mov    x21, x2              /* inslen*/
    mov    x22, x3              /* dellen*/
    mov    x23, x4              /* ins   */
    mov    x24, x5              /* del   */

    ldr    x9, [x19, #D_UNMAX]
    cbz    x9, .Lup_fail          /* no history configured at all */

    /* ---- try to merge into the live burst ------------------------------- */
    cbz    x22, .Lup_merge
    b      .Lup_new
.Lup_merge:
    ldr    x10, [x19, #D_BURST]
    tbnz   x10, #63, .Lup_new     /* no live burst */
    cmp    x10, x20
    b.ne   .Lup_new               /* not a continuation */
    ldr    x11, [x19, #D_UNC]
    cbz    x11, .Lup_new
    ldr    x12, [x19, #D_UNP]
    cmp    x11, x12
    b.ne   .Lup_new               /* can only merge the applied tip */
    sub    x12, x11, #1
    ldr    x13, [x19, #D_UENT]
    mov    x14, #U_SIZE
    madd   x13, x12, x14, x13     /* &entries[last] */
    /* payload must sit at the end of the arena so the bytes stay contiguous */
    ldr    x14, [x13, #U_INSPTR]
    ldr    x15, [x13, #U_INSLEN]
    add    x14, x14, x15
    ldr    x16, [x19, #D_UARENA]
    ldr    x17, [x19, #D_UAN]
    add    x16, x16, x17
    cmp    x14, x16
    b.ne   .Lup_new
    ldr    x16, [x19, #D_UACAP]
    sub    x16, x16, x17
    cmp    x16, x21
    b.lo   .Lup_reset
    /* append the bytes to the previous entry */
    ldr    x0, [x19, #D_UARENA]
    add    x0, x0, x17
    mov    x1, x23
    mov    x2, x21
    stp    x13, x13, [sp, #80]    /* keep the entry pointer across the call */
    bl     nota_memcpy
    ldr    x13, [sp, #80]
    ldr    x17, [x19, #D_UAN]
    add    x17, x17, x21
    str    x17, [x19, #D_UAN]
    ldr    x15, [x13, #U_INSLEN]
    add    x15, x15, x21
    str    x15, [x13, #U_INSLEN]
    /* the live burst now ends later */
    ldr    x12, [x13, #U_OFF]
    add    x12, x12, x15
    str    x12, [x19, #D_BURST]
    mov    x0, #1
    b      .Lup_out

.Lup_new:
    /* a new edit discards the redo tail */
    ldr    x10, [x19, #D_UNC]
    ldr    x11, [x19, #D_UNP]
    cmp    x10, x11
    csel   x10, x11, x10, ne
    /* need arena room for both payloads */
    ldr    x17, [x19, #D_UAN]
    add    x12, x21, x22
    ldr    x13, [x19, #D_UACAP]
    sub    x13, x13, x17
    cmp    x13, x12
    b.lo   .Lup_reset
    ldr    x14, [x19, #D_UNMAX]
    cmp    x10, x14
    b.hs   .Lup_reset             /* history full: reset rather than wrap */
    ldr    x13, [x19, #D_UENT]
    mov    x14, #U_SIZE
    madd   x13, x10, x14, x13     /* &entries[count] */
    str    x20, [x13, #U_OFF]
    str    x21, [x13, #U_INSLEN]
    str    x22, [x13, #U_DELLEN]
    /* inserted payload */
    ldr    x16, [x19, #D_UARENA]
    add    x16, x16, x17
    str    x16, [x13, #U_INSPTR]
    cbz    x21, 1f
    mov    x0, x16
    mov    x1, x23
    mov    x2, x21
    stp    x10, x13, [sp, #80]
    bl     nota_memcpy
    ldp    x10, x13, [sp, #80]
1:  add    x17, x17, x21
    /* removed payload */
    ldr    x16, [x19, #D_UARENA]
    add    x16, x16, x17
    str    x16, [x13, #U_DELPTR]
    cbz    x22, 2f
    mov    x0, x16
    mov    x1, x24
    mov    x2, x22
    stp    x10, x13, [sp, #80]
    bl     nota_memcpy
    ldp    x10, x13, [sp, #80]
2:  add    x17, x17, x22
    str    x17, [x19, #D_UAN]
    add    x10, x10, #1
    str    x10, [x19, #D_UNC]
    str    x10, [x19, #D_UNP]
    /* a pure insertion starts (or continues) a burst; anything else ends it */
    cbz    x22, 3f
    mov    x12, #-1
    str    x12, [x19, #D_BURST]
    b      4f
3:  add    x12, x20, x21
    str    x12, [x19, #D_BURST]
4:  mov    x0, #1
    b      .Lup_out
.Lup_reset:
    str    xzr, [x19, #D_UAN]
    str    xzr, [x19, #D_UNC]
    str    xzr, [x19, #D_UNP]
    mov    x12, #-1
    str    x12, [x19, #D_BURST]
.Lup_fail:
    mov    x0, #0
.Lup_out:
    ldr    x27, [sp, #80]
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #96
    ret
    .size nota_undo_push, . - nota_undo_push

/* void nota_undo_break(edit_doc *d)
 * Ends the live typing burst, so the next insertion is a separate undo step.
 * This only clears the burst marker; no entry fields are touched.
 */
    .globl nota_undo_break
    .type  nota_undo_break, %function
nota_undo_break:
    mov    x1, #-1
    str    x1, [x0, #D_BURST]
    ret
    .size nota_undo_break, . - nota_undo_break

/* ========================================================================= *
 * The replace primitive
 * ========================================================================= */

/* long nota_doc_replace_core(edit_doc *d, long start, long end,
 *                            const char *text, long len, long history)
 * history == 0 skips undo recording (used by undo/redo themselves).
 * Returns bytes inserted, EDIT_ERR_FULL or EDIT_ERR_RANGE.
 */
    .globl nota_doc_replace_core
    .type  nota_doc_replace_core, %function
nota_doc_replace_core:
    stp    x29, x30, [sp, #-112]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    stp    x27, x28, [sp, #80]
    /* x19 doc, x20 start, x21 end, x22 text, x23 len, x24 history */
    mov    x19, x0
    mov    x20, x1
    mov    x21, x2
    mov    x22, x3
    mov    x23, x4
    mov    x24, x5
    ldr    x9, [x19, #D_LEN]
    cmp    x21, x9
    b.hi   .Lrc_range
    cmp    x20, x21
    b.hi   .Lrc_range
    sub    x25, x21, x20          /* bytes to remove */
    /* capacity: len - removed + inserted <= cap - 1 */
    sub    x10, x9, x25
    add    x10, x10, x23
    ldr    x11, [x19, #D_CAP]
    sub    x11, x11, #1
    cmp    x10, x11
    b.hi   .Lrc_full

    /* undo entry before anything moves: it copies the removed bytes */
    cbz    x24, .Lrc_no_history
    cbz    x25, 1f
    /* a replacement ends the typing burst unless it is a pure insertion at
       the burst end, which nota_undo_push decides for itself */
1:  mov    x0, x19
    mov    x1, x20
    mov    x2, x23
    mov    x3, x25
    mov    x4, x22
    ldr    x5, [x19, #D_BUF]
    add    x5, x5, x20
    bl     nota_undo_push
.Lrc_no_history:
    ldr    x26, [x19, #D_BUF]      /* buf */
    ldr    x27, [x19, #D_LEN]
    sub    x27, x27, x21           /* tail bytes */
    cbz    x27, .Lrc_no_tail
    add    x0, x26, x20
    add    x0, x0, x23             /* dst = buf + start + inserted */
    add    x1, x26, x21            /* src = buf + end */
    mov    x2, x27
    bl     nota_memmove
.Lrc_no_tail:
    cbz    x23, .Lrc_no_ins
    add    x0, x26, x20
    mov    x1, x22
    mov    x2, x23
    bl     nota_memcpy
.Lrc_no_ins:
    ldr    x9, [x19, #D_LEN]
    sub    x9, x9, x25
    add    x9, x9, x23
    str    x9, [x19, #D_LEN]
    strb   wzr, [x26, x9]
    add    x10, x20, x23           /* caret after the inserted text */
    str    x10, [x19, #D_CURSOR]
    str    x10, [x19, #D_ANCH]
    str    xzr, [x19, #D_SELECTING]
    mov    x0, x26
    mov    x1, x9
    stp    x10, x10, [sp, #96]
    bl     nota_utf8_len
    ldp    x10, x11, [sp, #96]
    str    x0, [x19, #D_CPS]
    mov    x9, #1
    str    x9, [x19, #D_DIRTY]
    mov    x0, x23
    b      .Lrc_out
.Lrc_range:
    mov    x0, #EDIT_ERR_RANGE
    b      .Lrc_out
.Lrc_full:
    mov    x0, #EDIT_ERR_FULL
.Lrc_out:
    ldp    x27, x28, [sp, #80]
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #112
    ret
    .size nota_doc_replace_core, . - nota_doc_replace_core

/* long nota_doc_replace(edit_doc *d, long start, long end,
 *                       const char *text, long len)
 * The public primitive: records undo. Everything below calls this.
 */
    .globl nota_doc_replace
    .type  nota_doc_replace, %function
nota_doc_replace:
    mov    x5, #1
    b      nota_doc_replace_core
    .size nota_doc_replace, . - nota_doc_replace

/* long nota_doc_replace_no_history(edit_doc *d, long start, long end,
 *                                  const char *text, long len)
 * Used by undo and redo, which must not record themselves.
 */
    .globl nota_doc_replace_no_history
    .type  nota_doc_replace_no_history, %function
nota_doc_replace_no_history:
    mov    x5, #0
    b      nota_doc_replace_core
    .size nota_doc_replace_no_history, . - nota_doc_replace_no_history

/* void nota_doc_set_text(edit_doc *d, const char *text, long len)
 * A document load, not an edit: truncates at capacity and at a code point
 * boundary, and clears history and the dirty flag.
 */
    .globl nota_doc_set_text
    .type  nota_doc_set_text, %function
nota_doc_set_text:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    mov    x19, x0
    mov    x20, x1
    mov    x21, x2
    ldr    x2, [x19, #D_CAP]
    sub    x2, x2, #1
    cmp    x21, x2
    b.ls   1f
    mov    x21, x2
1:  mov    x0, x20
    mov    x1, x21
    bl     nota_utf8_trim
    mov    x21, x0
    ldr    x0, [x19, #D_BUF]
    mov    x1, x20
    mov    x2, x21
    bl     nota_memcpy
    ldr    x0, [x19, #D_BUF]
    strb   wzr, [x0, x21]
    str    x21, [x19, #D_LEN]
    mov    x1, x21
    bl     nota_utf8_len
    str    x0, [x19, #D_CPS]
    str    x21, [x19, #D_CURSOR]
    str    x21, [x19, #D_ANCH]
    str    xzr, [x19, #D_DIRTY]
    str    xzr, [x19, #D_SELECTING]
    mov    x0, x19
    bl     nota_undo_clear
    mov    x0, x21
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_doc_set_text, . - nota_doc_set_text

/* ========================================================================= *
 * Editing commands
 * ========================================================================= */

/* long nota_doc_insert(edit_doc *d, const char *text, long len)
 * Inserts at the caret, replacing the selection if there is one.
 */
    .globl nota_doc_insert
    .type  nota_doc_insert, %function
nota_doc_insert:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    mov    x19, x0
    mov    x20, x1              /* text */
    mov    x21, x2              /* len  */
    ldr    x1, [x19, #D_CURSOR]
    ldr    x2, [x19, #D_ANCH]
    cmp    x1, x2
    b.ls   1f
    mov    x9, x1               /* caret after anchor: start = anchor */
    mov    x1, x2
    mov    x2, x9
1:  mov    x0, x19
    mov    x3, x20
    mov    x4, x21
    bl     nota_doc_replace
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_doc_insert, . - nota_doc_insert

/* long nota_doc_insert_cp(edit_doc *d, unsigned cp) */
    .globl nota_doc_insert_cp
    .type  nota_doc_insert_cp, %function
nota_doc_insert_cp:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    mov    x19, x0
    mov    x20, x1
    add    x0, sp, #32
    mov    w1, w20
    bl     nota_utf8_encode
    cbz    x0, .Lic_bad
    mov    x2, x0
    mov    x0, x19
    add    x1, sp, #32
    bl     nota_doc_insert
    b      .Lic_out
.Lic_bad:
    mov    x0, #EDIT_ERR_CP
.Lic_out:
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_doc_insert_cp, . - nota_doc_insert_cp

/* long nota_doc_delete_selection(edit_doc *d) */
    .globl nota_doc_delete_selection
    .type  nota_doc_delete_selection, %function
nota_doc_delete_selection:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    mov    x19, x0
    ldr    x1, [x19, #D_CURSOR]
    ldr    x2, [x19, #D_ANCH]
    cmp    x1, x2
    b.eq   .Lds_none
    b.ls   1f
    mov    x9, x1
    mov    x1, x2
    mov    x2, x9
1:  sub    x20, x2, x1            /* bytes deleted */
    mov    x0, x19
    mov    x3, #0
    mov    x4, #0
    bl     nota_doc_replace
    mov    x0, x20
    b      .Lds_out
.Lds_none:
    mov    x0, #0
.Lds_out:
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_doc_delete_selection, . - nota_doc_delete_selection

/* long nota_doc_backspace(edit_doc *d) */
    .globl nota_doc_backspace
    .type  nota_doc_backspace, %function
nota_doc_backspace:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    mov    x19, x0
    ldr    x20, [x19, #D_CURSOR]
    ldr    x21, [x19, #D_ANCH]
    cmp    x20, x21
    b.ne   .Lbk_sel
    cbz    x20, .Lbk_none
    ldr    x0, [x19, #D_BUF]
    mov    x1, x20
    bl     nota_utf8_prev
    mov    x1, x0
    mov    x2, x20
    b      .Lbk_del
.Lbk_sel:
    cmp    x20, x21
    b.ls   1f
    mov    x9, x20
    mov    x20, x21
    mov    x21, x9
1:  mov    x1, x20
    mov    x2, x21
    sub    x20, x21, x20          /* reuse x20 as the byte count */
    add    x20, x1, x20
    b      .Lbk_del
.Lbk_del:
    /* x1 = start, x2 = end; remember the count across the call */
    sub    x21, x2, x1
    mov    x0, x19
    mov    x3, #0
    mov    x4, #0
    bl     nota_doc_replace
    mov    x0, x21
    b      .Lbk_out
.Lbk_none:
    mov    x0, #0
.Lbk_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_doc_backspace, . - nota_doc_backspace

/* long nota_doc_delete_forward(edit_doc *d) */
    .globl nota_doc_delete_forward
    .type  nota_doc_delete_forward, %function
nota_doc_delete_forward:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    mov    x19, x0
    ldr    x20, [x19, #D_CURSOR]
    ldr    x21, [x19, #D_ANCH]
    cmp    x20, x21
    b.ne   .Ldf_sel
    ldr    x1, [x19, #D_LEN]      /* nota_utf8_next(buf, len, off) */
    cmp    x20, x1
    b.hs   .Ldf_none
    ldr    x0, [x19, #D_BUF]
    mov    x2, x20
    bl     nota_utf8_next
    mov    x2, x0
    mov    x1, x20
    b      .Ldf_del
.Ldf_sel:
    cmp    x20, x21
    b.ls   1f
    mov    x9, x20
    mov    x20, x21
    mov    x21, x9
1:  mov    x1, x20
    mov    x2, x21
.Ldf_del:
    sub    x21, x2, x1
    mov    x0, x19
    mov    x3, #0
    mov    x4, #0
    bl     nota_doc_replace
    mov    x0, x21
    b      .Ldf_out
.Ldf_none:
    mov    x0, #0
.Ldf_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_doc_delete_forward, . - nota_doc_delete_forward

/* ========================================================================= *
 * Caret movement
 * ========================================================================= */

/* void nota_doc_move(edit_doc *d, long newoff, long extend) */
    .globl nota_doc_move
    .type  nota_doc_move, %function
nota_doc_move:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    mov    x19, x0
    mov    x20, x2
    str    x1, [sp, #40]
    ldr    x0, [x19, #D_BUF]
    ldr    x1, [x19, #D_LEN]
    add    x2, sp, #40
    bl     nota_clamp_cursor
    ldr    x21, [sp, #40]
    cbnz   x20, .Ldm_extend
    str    x21, [x19, #D_CURSOR]
    str    x21, [x19, #D_ANCH]
    str    xzr, [x19, #D_SELECTING]
    mov    x0, x19              /* nota_undo_break takes the document */
    bl     nota_undo_break
    b      .Ldm_out
.Ldm_extend:
    /* Extending never crosses the anchor: a selection cannot flip sides.
       Moving past it is the same as moving onto it. */
    ldr    x9, [x19, #D_ANCH]
    ldr    x10, [x19, #D_CURSOR]
    cmp    x10, x9
    b.hs   1f                   /* caret is at/after the anchor: moving right */
    /* caret is before the anchor: the new offset may not exceed the anchor */
    cmp    x21, x9
    csel   x21, x21, x9, lo
    b      2f
1:  /* caret at/after the anchor: the new offset may not fall below it */
    cmp    x21, x9
    csel   x21, x21, x9, hs
2:  str    x21, [x19, #D_CURSOR]
.Ldm_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_doc_move, . - nota_doc_move

/* void nota_doc_move_left(edit_doc *d, long extend)
 * With a selection and no extend, collapses to the selection start; with
 * extend, grows the selection one code point to the left.
 */
    .globl nota_doc_move_left
    .type  nota_doc_move_left, %function
nota_doc_move_left:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    mov    x19, x0
    mov    x20, x1
    ldr    x9, [x19, #D_CURSOR]
    ldr    x10, [x19, #D_ANCH]
    cmp    x9, x10
    b.eq   .Lml_one
    cbz    x20, .Lml_collapse
.Lml_one:
    /* With a selection and extend, walk back from the caret itself; with an
       empty selection that is the same thing. */
    mov    x1, x9
    ldr    x0, [x19, #D_BUF]
    bl     nota_utf8_prev
    mov    x1, x0
    mov    x0, x19
    mov    x2, x20
    bl     nota_doc_move
    b      .Lml_out
.Lml_collapse:
    cmp    x9, x10
    csel   x1, x9, x10, lo
    mov    x0, x19
    mov    x2, #0
    bl     nota_doc_move
.Lml_out:
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_doc_move_left, . - nota_doc_move_left

/* void nota_doc_move_right(edit_doc *d, long extend) */
    .globl nota_doc_move_right
    .type  nota_doc_move_right, %function
nota_doc_move_right:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    mov    x19, x0
    mov    x20, x1
    ldr    x9, [x19, #D_CURSOR]
    ldr    x10, [x19, #D_ANCH]
    cmp    x9, x10
    b.eq   .Lmr_one
    cbz    x20, .Lmr_collapse
.Lmr_one:
    /* Extending walks forward from the caret itself, so a selection that
       started to the left can be shrunk again. */
    mov    x2, x9
    ldr    x0, [x19, #D_BUF]
    ldr    x1, [x19, #D_LEN]
    bl     nota_utf8_next
    mov    x1, x0
    mov    x0, x19
    mov    x2, x20
    bl     nota_doc_move
    b      .Lmr_out
.Lmr_collapse:
    cmp    x9, x10
    csel   x1, x9, x10, hs
    mov    x0, x19
    mov    x2, #0
    bl     nota_doc_move
.Lmr_out:
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_doc_move_right, . - nota_doc_move_right

/* void nota_doc_move_line_start(edit_doc *d, long extend) */
    .globl nota_doc_move_line_start
    .type  nota_doc_move_line_start, %function
nota_doc_move_line_start:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    mov    x19, x0
    mov    x20, x1
    ldr    x0, [x19, #D_BUF]
    ldr    x1, [x19, #D_CURSOR]
    bl     nota_line_start
    mov    x1, x0
    mov    x0, x19
    mov    x2, x20
    bl     nota_doc_move
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_doc_move_line_start, . - nota_doc_move_line_start

/* void nota_doc_move_line_end(edit_doc *d, long extend) */
    .globl nota_doc_move_line_end
    .type  nota_doc_move_line_end, %function
nota_doc_move_line_end:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    mov    x19, x0
    mov    x20, x1
    ldr    x0, [x19, #D_BUF]
    ldr    x1, [x19, #D_LEN]
    ldr    x2, [x19, #D_CURSOR]
    bl     nota_line_end
    mov    x1, x0
    mov    x0, x19
    mov    x2, x20
    bl     nota_doc_move
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_doc_move_line_end, . - nota_doc_move_line_end

/* void nota_doc_move_vert(edit_doc *d, long dir, long extend)
 * dir < 0 up, dir > 0 down. The column is preserved in bytes from the line
 * start, which is what makes walking through short lines feel natural.
 */
    .globl nota_doc_move_vert
    .type  nota_doc_move_vert, %function
nota_doc_move_vert:
    stp    x29, x30, [sp, #-64]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    str    x23, [sp, #48]
    mov    x19, x0
    mov    x20, x1
    mov    x21, x2
    ldr    x22, [x19, #D_CURSOR]
    ldr    x0, [x19, #D_BUF]
    mov    x1, x22                /* nota_line_start(buf, off) */
    bl     nota_line_start
    mov    x9, x0                 /* current line start */
    /* The column is remembered across consecutive vertical moves, so walking
       down through a short line does not permanently lose the column. The
       memory is valid only while the caret is still where the last vertical
       move left it. */
    ldr    x11, [x19, #D_GOALSET]
    cmp    x11, x22
    b.ne   .Lmv_measure
    ldr    x23, [x19, #D_GOALCOL]
    b      .Lmv_have_col
.Lmv_measure:
    sub    x23, x22, x9
    str    x23, [x19, #D_GOALCOL]
.Lmv_have_col:
    tbnz   x20, #63, .Lmv_up
    /* down: end of the current line, then past its newline */
    ldr    x0, [x19, #D_BUF]
    ldr    x1, [x19, #D_LEN]
    mov    x2, x22
    bl     nota_line_end
    ldr    x1, [x19, #D_LEN]
    cmp    x0, x1
    b.hs   .Lmv_out               /* last line: nothing below */
    add    x10, x0, #1
    b      .Lmv_place
.Lmv_up:
    cbz    x9, .Lmv_out           /* first line: nothing above */
    ldr    x0, [x19, #D_BUF]
    sub    x1, x9, #1             /* the newline ending the previous line */
    bl     nota_line_start
    mov    x10, x0
.Lmv_place:
    /* target = min(line_start + column, line_end). x10 (the target line's
       start) must survive the line_end call, so keep it in x11. */
    mov    x11, x10
    ldr    x0, [x19, #D_BUF]
    ldr    x1, [x19, #D_LEN]
    mov    x2, x11
    bl     nota_line_end
    add    x12, x11, x23
    cmp    x12, x0
    csel   x12, x12, x0, lo
    mov    x1, x12
    mov    x0, x19
    mov    x2, x21
    bl     nota_doc_move
    ldr    x9, [x19, #D_CURSOR]
    str    x9, [x19, #D_GOALSET]
.Lmv_out:
    ldr    x23, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #64
    ret
    .size nota_doc_move_vert, . - nota_doc_move_vert

/* void nota_doc_select_all(edit_doc *d) */
    .globl nota_doc_select_all
    .type  nota_doc_select_all, %function
nota_doc_select_all:
    str    xzr, [x0, #D_ANCH]
    ldr    x1, [x0, #D_LEN]
    str    x1, [x0, #D_CURSOR]
    mov    x1, #1
    str    x1, [x0, #D_SELECTING]
    ret
    .size nota_doc_select_all, . - nota_doc_select_all

/* long nota_is_word_byte(unsigned b, int at_start)
 * 1 if the byte can belong to a word. ASCII punctuation and whitespace are
 * separators; all non-ASCII bytes count as letters so CJK and accented words
 * select as one unit.
 */
    .globl nota_is_word_byte
    .type  nota_is_word_byte, %function
nota_is_word_byte:
    and    w0, w0, #0xff
    cmp    w0, #0x80
    b.hs   .Liw_yes
    cmp    w0, #'0'
    b.lo   1f
    cmp    w0, #'9'
    b.ls   .Liw_yes
1:  cmp    w0, #'A'
    b.lo   2f
    cmp    w0, #'Z'
    b.ls   .Liw_yes
2:  cmp    w0, #'a'
    b.lo   3f
    cmp    w0, #'z'
    b.ls   .Liw_yes
3:  cmp    w0, #'_'
    b.eq   .Liw_yes
    mov    x0, #0
    ret
.Liw_yes:
    mov    x0, #1
    ret
    .size nota_is_word_byte, . - nota_is_word_byte

/* void nota_doc_select_word_at(edit_doc *d, long off) - double-tap select */
    .globl nota_doc_select_word_at
    .type  nota_doc_select_word_at, %function
nota_doc_select_word_at:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    mov    x19, x0
    ldr    x20, [x19, #D_BUF]
    ldr    x21, [x19, #D_LEN]
    cmp    x1, x21
    csel   x1, x1, x21, lo
    mov    x22, x1               /* the tapped offset */
    /* left */
    mov    x9, x22
.Lsw_left:
    cbz    x9, 1f
    ldrb   w0, [x20, x9]
    bl     nota_is_word_byte
    cbz    x0, 1f
    sub    x9, x9, #1
    b      .Lsw_left
1:  mov    x10, x9
    /* the byte at x9 is a separator; the word starts after it */
    cbz    x9, 2f
    add    x9, x9, #1
    b      3f
2:  mov    x9, #0
3:  /* right */
    mov    x11, x22
.Lsw_right:
    cmp    x11, x21
    b.hs   4f
    ldrb   w0, [x20, x11]
    bl     nota_is_word_byte
    cbz    x0, 4f
    add    x11, x11, #1
    b      .Lsw_right
4:  str    x9, [x19, #D_ANCH]
    str    x11, [x19, #D_CURSOR]
    mov    x0, #1
    str    x0, [x19, #D_SELECTING]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_doc_select_word_at, . - nota_doc_select_word_at

/* ========================================================================= *
 * Undo / redo
 * ========================================================================= */

/* long nota_doc_undo(edit_doc *d) - 1 if something was undone */
    .globl nota_doc_undo
    .type  nota_doc_undo, %function
nota_doc_undo:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    mov    x19, x0
    ldr    x20, [x19, #D_UNP]
    cbz    x20, .Lu_none
    sub    x20, x20, #1
    str    x20, [x19, #D_UNP]
    ldr    x21, [x19, #D_UENT]
    mov    x9, #U_SIZE
    madd   x21, x20, x9, x21
    /* replace [off, off+inslen) with the removed bytes */
    ldr    x1, [x21, #U_OFF]
    ldr    x9, [x21, #U_INSLEN]
    add    x2, x1, x9
    ldr    x3, [x21, #U_DELPTR]
    ldr    x4, [x21, #U_DELLEN]
    mov    x0, x19
    bl     nota_doc_replace_no_history
    /* the caret sits at the end of the restored text */
    ldr    x9, [x21, #U_OFF]
    ldr    x10, [x21, #U_DELLEN]
    add    x9, x9, x10
    str    x9, [x19, #D_CURSOR]
    str    x9, [x19, #D_ANCH]
    mov    x0, x19
    bl     nota_undo_break
    mov    x9, #1
    str    x9, [x19, #D_DIRTY]
    mov    x0, #1
    b      .Lu_out
.Lu_none:
    mov    x0, #0
.Lu_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_doc_undo, . - nota_doc_undo

/* long nota_doc_redo(edit_doc *d) - 1 if something was redone */
    .globl nota_doc_redo
    .type  nota_doc_redo, %function
nota_doc_redo:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    mov    x19, x0
    ldr    x20, [x19, #D_UNP]
    ldr    x9, [x19, #D_UNC]
    cmp    x20, x9
    b.hs   .Lr_none
    ldr    x21, [x19, #D_UENT]
    mov    x10, #U_SIZE
    madd   x21, x20, x10, x21
    add    x20, x20, #1
    str    x20, [x19, #D_UNP]
    /* replace the removed bytes with the inserted ones */
    ldr    x1, [x21, #U_OFF]
    ldr    x9, [x21, #U_DELLEN]
    add    x2, x1, x9
    ldr    x3, [x21, #U_INSPTR]
    ldr    x4, [x21, #U_INSLEN]
    mov    x0, x19
    bl     nota_doc_replace_no_history
    ldr    x9, [x21, #U_OFF]
    ldr    x10, [x21, #U_INSLEN]
    add    x9, x9, x10
    str    x9, [x19, #D_CURSOR]
    str    x9, [x19, #D_ANCH]
    mov    x0, x19
    bl     nota_undo_break
    mov    x9, #1
    str    x9, [x19, #D_DIRTY]
    mov    x0, #1
    b      .Lr_out
.Lr_none:
    mov    x0, #0
.Lr_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_doc_redo, . - nota_doc_redo

/* long nota_doc_can_undo(edit_doc *d) */
    .globl nota_doc_can_undo
    .type  nota_doc_can_undo, %function
nota_doc_can_undo:
    ldr    x1, [x0, #D_UNP]
    cmp    x1, #0
    cset   x0, ne
    ret
    .size nota_doc_can_undo, . - nota_doc_can_undo

/* long nota_doc_can_redo(edit_doc *d) */
    .globl nota_doc_can_redo
    .type  nota_doc_can_redo, %function
nota_doc_can_redo:
    ldr    x1, [x0, #D_UNP]
    ldr    x2, [x0, #D_UNC]
    cmp    x1, x2
    cset   x0, lo
    ret
    .size nota_doc_can_redo, . - nota_doc_can_redo

    .section .note.GNU-stack,"",%progbits
