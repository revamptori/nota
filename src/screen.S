/*
 * screen.S - painting the two screens.
 *
 * Both painters are pure: they read the note array, the editor document and
 * the layout struct, and emit quads. No GL calls of their own beyond what
 * ui.S wraps, so executing them on the test interpreter fills
 * nota_gl_vbuf with exactly the geometry a device would rasterise.
 */
#include "nota.h"
#include "macros.inc"

    .equ UI_SCR,      0x00
    .equ UI_W,        0x08
    .equ UI_H,        0x10
    .equ UI_SCROLL,   0x18
    .equ UI_ROWH,     0x20
    .equ UI_ROWS,     0x28
    .equ UI_TEXTX,    0x30
    .equ UI_TOPY,     0x38
    .equ UI_BTN_Y,    0x40
    .equ UI_BTN_X,    0x48
    .equ UI_BTN_S,    0x50
    .equ UI_EDIT_TOP, 0x58
    .equ UI_WRAPX,    0x60
    .equ UI_LINEH,    0x68

    .equ PAD,         16
    .equ ROWH,        96
    .equ HEADER_H,    72
    .equ ROW_TEXTX,   76
    .equ TITLE_BASE,  40
    .equ PREVIEW_BASE, 72
    .equ EDIT_PADX,   20

    .text

/* ========================================================================= *
 * void nota_screen_list(void)
 * Header, then only the rows that intersect the viewport, then the button.
 * ========================================================================= */
    .globl nota_screen_list
    .type  nota_screen_list, %function
nota_screen_list:
    stp    x29, x30, [sp, #-96]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    adrp   x19, nota_ui
    add    x19, x19, :lo12:nota_ui
    adrp   x20, nota_store
    add    x20, x20, :lo12:nota_store
    mov    x24, #0x08              /* ST_COUNT */
    mov    x26, #NOTE_SIZE

    /* ---- header ------------------------------------------------------- */
    mov    x0, #0
    mov    x1, #0
    ldr    x2, [x19, #UI_W]
    mov    x3, #HEADER_H
    mov32  w4, COL_PANEL
    bl     nota_ui_fill

    /* "NOTA" in the accent colour */
    adrp   x9, .Lstr_nota
    add    x9, x9, :lo12:.Lstr_nota
    mov    x0, x9
    mov    x1, #4
    mov    x2, #PAD
    mov    x3, #46
    mov32  w4, COL_ACCENT
    bl     nota_ui_text

    /* note count, right aligned, dim */
    ldr    x9, [x20, x24]
    adrp   x10, nota_num
    add    x10, x10, :lo12:nota_num
    mov    x0, x10              /* dst: nota_num */
    mov    x1, x9               /* value: the count */
    bl     nota_utoa
    mov    x21, x0              /* digit count */
    mov    x0, x10
    mov    x1, x21
    bl     nota_text_width
    ldr    x9, [x19, #UI_W]
    sub    x9, x9, x0
    sub    x9, x9, #PAD
    adrp   x10, nota_num
    add    x10, x10, :lo12:nota_num
    mov    x0, x10
    mov    x1, x21
    mov    x2, x9
    mov    x3, #46
    mov32  w4, COL_TEXT_DIM
    bl     nota_ui_text

    /* ---- rows --------------------------------------------------------- */
    ldr    x21, [x19, #UI_SCROLL]      /* row */
    ldr    x25, [x19, #UI_ROWS]
    ldr    x9, [x19, #UI_SCROLL]
    add    x25, x25, x9                /* first row past the viewport */
.Lsl_row:
    cmp    x21, x25
    b.hs   .Lsl_rows_done
    ldr    x9, [x20, x24]
    cmp    x21, x9
    b.hs   .Lsl_rows_done

    mov    x0, x21
    bl     nota_ui_row_y
    mov    x22, x0                     /* row top */

    /* note = notes + row * NOTE_SIZE */
    ldr    x10, [x20, #0x00]           /* ST_NOTES */
    mul    x11, x21, x26
    add    x23, x10, x11               /* &notes[row] */

    /* a card per row, inset by PAD, with a hairline gap */
    ldr    x0, [x19, #UI_W]
    sub    x2, x0, #(PAD * 2)
    mov    x0, #PAD
    add    x1, x22, #4
    ldr    x3, [x19, #UI_ROWH]
    sub    x3, x3, #8
    mov    x4, #10
    mov32  w5, COL_PANEL
    bl     nota_ui_round_rect

    /* title: pinned/trashed notes get a colour cue, not a different layout */
    ldr    w9, [x23, #NOTE_FLAGS]
    mov32  w4, COL_TEXT
    tbz    w9, #0, 1f                  /* NOT pinned */
    mov32  w4, COL_ACCENT
1:  tbnz   w9, #1, 2f                  /* trashed */
    b      3f
2:  mov32  w4, COL_DANGER
3:  ldr    x0, [x23, #NOTE_TITLE_PTR]
    ldr    w1, [x23, #NOTE_TITLE_LEN]
    mov    x2, #ROW_TEXTX
    add    x3, x22, #TITLE_BASE
    ldr    x5, [x19, #UI_W]
    sub    x5, x5, #PAD
    bl     nota_ui_text_clip

    /* Preview: the body is the whole document, whose first line *is* the
       title, so the preview has to start past that line or every card shows
       its title twice. Skip the title's length, then any newline and the
       indentation of the next line, so the preview starts on real text. */
    ldr    x0, [x23, #NOTE_BODY_PTR]
    ldr    w1, [x23, #NOTE_BODY_LEN]
    ldr    w9, [x23, #NOTE_TITLE_LEN]
    cmp    x1, x9
    b.ls   .Lsl_no_preview             /* title is the whole body */
    sub    x1, x1, x9                  /* body_len - title_len */
    add    x0, x0, x9
    /* one past the line break, if there is one */
.Lsl_skip_nl:
    cbz    x1, .Lsl_no_preview
    ldrb   w10, [x0]
    cmp    w10, #0x0A
    b.ne   1f
    add    x0, x0, #1
    sub    x1, x1, #1
    b      .Lsl_skip_nl
1:  cmp    w10, #0x0D
    b.ne   .Lsl_preview_go
    add    x0, x0, #1
    sub    x1, x1, #1
    b      .Lsl_skip_nl
.Lsl_preview_go:
    mov    x2, #ROW_TEXTX
    add    x3, x22, #PREVIEW_BASE
    ldr    x5, [x19, #UI_W]
    sub    x5, x5, #PAD
    mov32  w4, COL_TEXT_DIM
    bl     nota_ui_text_clip
.Lsl_no_preview:
    add    x21, x21, #1
    b      .Lsl_row
.Lsl_rows_done:

    /* ---- empty state -------------------------------------------------- */
    ldr    x9, [x20, x24]
    cbnz   x9, .Lsl_button
    adrp   x9, .Lstr_empty
    add    x9, x9, :lo12:.Lstr_empty
    mov    x0, x9
    mov    x1, #19
    mov    x2, #PAD
    ldr    x3, [x19, #UI_TOPY]
    add    x3, x3, #48
    ldr    x5, [x19, #UI_W]
    mov32  w4, COL_TEXT_DIM
    bl     nota_ui_text_clip

.Lsl_button:
    /* the new-note button: accent disc with a plus, drawn from rects so it
       does not need a glyph the font does not carry */
    ldr    x0, [x19, #UI_BTN_X]
    ldr    x1, [x19, #UI_BTN_Y]
    ldr    x2, [x19, #UI_BTN_S]
    mov    x3, x2
    mov    x4, #28
    mov32  w5, COL_ACCENT
    bl     nota_ui_round_rect
    /* plus, centred */
    ldr    x0, [x19, #UI_BTN_X]
    ldr    x2, [x19, #UI_BTN_S]
    add    x0, x0, x2, lsr #1
    sub    x0, x0, #1
    ldr    x1, [x19, #UI_BTN_Y]
    add    x1, x1, #14
    mov    x2, #3
    ldr    x3, [x19, #UI_BTN_S]
    sub    x3, x3, #28
    mov32  w4, COL_BG
    bl     nota_ui_fill
    ldr    x0, [x19, #UI_BTN_X]
    add    x0, x0, #14
    ldr    x1, [x19, #UI_BTN_Y]
    ldr    x2, [x19, #UI_BTN_S]
    add    x1, x1, x2, lsr #1
    sub    x1, x1, #1
    ldr    x2, [x19, #UI_BTN_S]
    sub    x2, x2, #28
    mov    x3, #3
    mov32  w4, COL_BG
    bl     nota_ui_fill

    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #96
    ret
    .size nota_screen_list, . - nota_screen_list

/* ========================================================================= *
 * void nota_screen_edit(void)
 * Header with the title, then wrapped body lines, the caret and any
 * selection band.
 * ========================================================================= */
    .globl nota_screen_edit
    .type  nota_screen_edit, %function
nota_screen_edit:
    stp    x29, x30, [sp, #-96]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    stp    x27, x28, [sp, #80]
    adrp   x19, nota_ui
    add    x19, x19, :lo12:nota_ui
    adrp   x20, nota_doc
    add    x20, x20, :lo12:nota_doc
    adrp   x21, nota_edit
    add    x21, x21, :lo12:nota_edit
    /* ED_NOTE is field 0; the offset is a cross-object relocation, so read
       through the base pointer rather than with an immediate. */

    /* ---- header ------------------------------------------------------- */
    mov    x0, #0
    mov    x1, #0
    ldr    x2, [x19, #UI_W]
    mov    x3, #HEADER_H
    mov32  w4, COL_PANEL
    bl     nota_ui_fill

    /* back chevron, drawn as two thin diagonal-free bars */
    mov    x0, #PAD
    add    x0, x0, #6
    mov    x1, #30
    mov    x2, #3
    mov    x3, #13
    mov32  w4, COL_ACCENT
    bl     nota_ui_fill

    /* title of the note being edited */
    ldr    x9, [x21]
    cbz    x9, .Lse_body
    ldr    x0, [x9, #NOTE_TITLE_PTR]
    ldr    w1, [x9, #NOTE_TITLE_LEN]
    mov    x2, #(PAD + 40)
    mov    x3, #46
    mov32  w4, COL_TEXT
    ldr    x5, [x19, #UI_W]
    sub    x5, x5, #PAD
    bl     nota_ui_text_clip

.Lse_body:
    /* The document buffer and length must survive every call below, so they
       live in callee-saved registers. x21/x22 were used for nota_ui and
       nota_doc before this point and are free now. */
    ldr    x21, [x20, #DOC_BUF]     /* buffer */
    ldr    x22, [x20, #DOC_LEN]     /* length */
    /* nota_ui_wrap takes a *line width*, not a right edge. WRAPX is the
       absolute x the text must stop at, so subtract the left inset here;
       passing the edge itself let a 944px line start at x=16 and run to 1071,
       past the margin the editor is supposed to respect. */
    mov    x0, x21
    mov    x1, x22
    ldr    x2, [x19, #UI_WRAPX]
    sub    x2, x2, #EDIT_PADX
    bl     nota_ui_wrap
    mov    x23, x0                  /* line count */

    mov    x24, #0                  /* line index */
.Lse_line:
    cmp    x24, x23
    b.hs   .Lse_caret
    adrp   x25, nota_wrap
    add    x25, x25, :lo12:nota_wrap
    ldr    x26, [x25, x24, lsl #3]  /* this line's start address */
    add    x9, x24, #1
    cmp    x9, x23
    b.hs   1f
    ldr    x27, [x25, x9, lsl #3]   /* next line's start address */
    b      2f
1:  add    x27, x21, x22            /* last line runs to the end of the buffer */
2:  /* A hard-wrapped line ends in '\n', which is not drawn. */
    cmp    x27, x26
    b.ls   3f
    sub    x9, x27, #1
    ldrb   w10, [x9]
    cmp    w10, #10
    b.ne   3f
    mov    x27, x9
3:  mov    x0, x26
    sub    x1, x27, x26
    mov    x2, #EDIT_PADX
    ldr    x3, [x19, #UI_EDIT_TOP]
    ldr    x9, [x19, #UI_LINEH]
    madd   x3, x24, x9, x3
    ldr    x5, [x19, #UI_W]
    sub    x5, x5, #EDIT_PADX
    mov32  w4, COL_TEXT
    bl     nota_ui_text_clip
    add    x24, x24, #1
    b      .Lse_line

.Lse_caret:
    /* Which wrapped line holds the caret? Then the pen x at the caret. */
    ldr    x26, [x20, #DOC_CURSOR]
    adrp   x25, nota_wrap
    add    x25, x25, :lo12:nota_wrap
    mov    x24, #0
.Lse_find:
    add    x9, x24, #1
    cmp    x9, x23
    b.hs   .Lse_found
    ldr    x10, [x25, x9, lsl #3]
    cmp    x10, x26
    b.hi   .Lse_found
    mov    x24, x9
    b      .Lse_find
.Lse_found:
    ldr    x27, [x25, x24, lsl #3]  /* line start address */
    mov    x0, x27
    sub    x1, x26, x27
    bl     nota_text_width
    add    x0, x0, #EDIT_PADX
    mov    x9, x0
    ldr    x10, [x19, #UI_EDIT_TOP]
    ldr    x11, [x19, #UI_LINEH]
    madd   x10, x24, x11, x10
    sub    x10, x10, #20            /* top of the line box */
    mov    x0, x9
    mov    x1, x10
    mov    x2, #2
    mov    x3, #24
    mov32  w4, COL_CURSOR
    bl     nota_ui_fill

    ldp    x27, x28, [sp, #80]
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #96
    ret
    .size nota_screen_edit, . - nota_screen_edit

    .section .rodata
.Lstr_nota:  .asciz "NOTA"
.Lstr_empty: .asciz "No notes yet. Tap +."

    .section .note.GNU-stack,"",%progbits
