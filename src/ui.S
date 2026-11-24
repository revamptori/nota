/*
 * ui.S - layout, drawing and hit-testing.
 *
 * Two screens share one geometry struct (nota_ui). Layout is a pure function
 * of the surface size and is recomputed during drawing; hit-testing reads the
 * same struct, so a tap can never disagree with what was drawn - there is no
 * second copy of the geometry that can drift.
 *
 * Drawing writes quads through nota_gl_quad, which appends to nota_gl_vbuf.
 * That means the draw path is pure data assembly and can be executed and
 * inspected on the test interpreter with GL stubbed out: a wrong rect shows
 * up as wrong bytes, not as a wrong picture somebody has to notice.
 */
#include "nota.h"

    .equ SCR_LIST,   0
    .equ SCR_EDIT,   1

/* ---------------------------------------------------------- ui state ------ */
    .equ UI_SCR,      0x00   /* long: SCR_*                                 */
    .equ UI_W,        0x08   /* long: surface size                          */
    .equ UI_H,        0x10
    .equ UI_SCROLL,   0x18   /* long: first visible row                     */
    .equ UI_ROWH,     0x20   /* long: row height                            */
    .equ UI_ROWS,     0x28   /* long: rows that fit vertically              */
    .equ UI_TEXTX,    0x30   /* long: left edge of row text                 */
    .equ UI_TOPY,     0x38   /* long: top of the first row                  */
    .equ UI_BTN_Y,    0x40   /* long: top of the new-note button            */
    .equ UI_BTN_X,    0x48
    .equ UI_BTN_S,    0x50   /* long: button size                           */
    .equ UI_EDIT_TOP, 0x58   /* long: first body line's baseline            */
    .equ UI_WRAPX,    0x60   /* long: right edge text may reach             */
    .equ UI_LINEH,    0x68   /* long: editor line height                    */
    .equ UI_SIZE,     0x78

    .equ PAD,         16
    .equ ROWH,        96
    .equ HEADER_H,    72
    .equ ROW_TEXTX,   76
    .equ TITLE_BASE,  40
    .equ PREVIEW_BASE, 72
    .equ BTN_SIZE,    56
    .equ EDIT_LINEH,  26
    .equ EDIT_PADX,   20

static_geom:
    .section .bss
    .p2align 4
    .globl nota_ui
    .hidden nota_ui
    .type  nota_ui, %object
nota_ui:
    .space UI_SIZE
    .size nota_ui, . - nota_ui

    /* Wrapped line start offsets for the editor, as long per line. */
    .globl nota_wrap
    .hidden nota_wrap
    .type  nota_wrap, %object
nota_wrap:
    .space 8 * 4096
    .size nota_wrap, . - nota_wrap

    .globl nota_wrap_n
    .hidden nota_wrap_n
    .type  nota_wrap_n, %object
nota_wrap_n:
    .space 8
    .size nota_wrap_n, . - nota_wrap_n

    .text

/* ========================================================================= *
 * long nota_isqrt(long v) - floor(sqrt(v)), v >= 0. Newton from above.
 * ========================================================================= */
    .globl nota_isqrt
    .type  nota_isqrt, %function
nota_isqrt:
    /* Integer square root by Newton's method, iterating until the estimate
       stops improving. A fixed iteration count is not enough: the same count
       that is ample for 65536 is too few for 10^9, where the estimate is still
       ~20% high when the counter runs out. */
    cmp    x0, #1
    b.hi   1f
    ret
1:  lsr    x9, x0, #1
    add    x9, x9, #1              /* first guess: n/2 + 1 */
2:  udiv   x11, x0, x9
    add    x11, x11, x9
    lsr    x11, x11, #1            /* next = (g + n/g) / 2 */
    cmp    x11, x9
    b.hs   3f                      /* no longer improving */
    mov    x9, x11
    b      2b
3:  mul    x10, x9, x9
    cmp    x10, x0
    b.ls   4f
    sub    x9, x9, #1              /* never overshoot */
4:  mov    x0, x9
    ret
    .size nota_isqrt, . - nota_isqrt

/* ========================================================================= *
 * void nota_ui_layout(long w, long h)
 * Everything the two screens need, derived from the surface size.
 * ========================================================================= */
    .globl nota_ui_layout
    .type  nota_ui_layout, %function
nota_ui_layout:
    adrp   x9, nota_ui
    add    x9, x9, :lo12:nota_ui
    str    x0, [x9, #UI_W]
    str    x1, [x9, #UI_H]

    /* list: fixed row height under a header */
    mov    w12, #ROWH
    str    x12, [x9, #UI_ROWH]
    mov    w10, #HEADER_H
    str    x10, [x9, #UI_TOPY]
    /* A surface shorter than the header leaves a negative body height.
       udiv is unsigned, so dividing that gives a nonsense row count that
       overflows every later loop bound; clamp to zero first. */
    sub    x11, x1, x10
    mov    x13, #0
    cmp    x11, x13
    csel   x11, xzr, x11, le
    udiv   x11, x11, x12
    cmp    x11, #1
    b.hs   1f
    mov    x11, #1
1:  str    x11, [x9, #UI_ROWS]
    mov    w10, #ROW_TEXTX
    str    x10, [x9, #UI_TEXTX]

    /* new-note button: bottom right, inset by PAD */
    mov    w10, #BTN_SIZE
    str    x10, [x9, #UI_BTN_S]
    sub    x11, x0, x10
    sub    x11, x11, #PAD
    str    x11, [x9, #UI_BTN_X]
    sub    x11, x1, x10
    sub    x11, x11, #PAD
    str    x11, [x9, #UI_BTN_Y]

    /* editor: body starts under its own header, text may run to the right
       margin, and wraps at that edge */
    mov    w10, #HEADER_H
    add    x10, x10, #EDIT_LINEH
    str    x10, [x9, #UI_EDIT_TOP]
    mov    w10, #EDIT_LINEH
    str    x10, [x9, #UI_LINEH]
    sub    x11, x0, #EDIT_PADX
    sub    x11, x11, #EDIT_PADX
    str    x11, [x9, #UI_WRAPX]
    ret
    .size nota_ui_layout, . - nota_ui_layout

/* ========================================================================= *
 * void nota_ui_set_screen(long screen)
 * ========================================================================= */
    .globl nota_ui_set_screen
    .type  nota_ui_set_screen, %function
nota_ui_set_screen:
    adrp   x9, nota_ui
    add    x9, x9, :lo12:nota_ui
    str    x0, [x9, #UI_SCR]
    ret
    .size nota_ui_set_screen, . - nota_ui_set_screen

/* ========================================================================= *
 * long nota_ui_screen(void)
 * ========================================================================= */
    .globl nota_ui_screen
    .type  nota_ui_screen, %function
nota_ui_screen:
    adrp   x9, nota_ui
    add    x9, x9, :lo12:nota_ui
    ldr    x0, [x9, #UI_SCR]
    ret
    .size nota_ui_screen, . - nota_ui_screen

/* ========================================================================= *
 * long nota_ui_row_at(long y) - row index under a screen y, or -1.
 * ========================================================================= */
    .globl nota_ui_row_at
    .type  nota_ui_row_at, %function
nota_ui_row_at:
    adrp   x9, nota_ui
    add    x9, x9, :lo12:nota_ui
    ldr    x10, [x9, #UI_TOPY]
    cmp    x0, x10
    b.lo   .Lra_none
    sub    x11, x0, x10
    mov    w12, #ROWH
    udiv   x13, x11, x12
    ldr    x14, [x9, #UI_ROWS]
    cmp    x13, x14
    b.hs   .Lra_none
    ldr    x15, [x9, #UI_SCROLL]
    add    x0, x13, x15
    ret
.Lra_none:
    mov    x0, #-1
    ret
    .size nota_ui_row_at, . - nota_ui_row_at

/* ========================================================================= *
 * long nota_ui_row_y(long row) - top edge of a row, in screen pixels.
 * ========================================================================= */
    .globl nota_ui_row_y
    .type  nota_ui_row_y, %function
nota_ui_row_y:
    adrp   x9, nota_ui
    add    x9, x9, :lo12:nota_ui
    ldr    x10, [x9, #UI_SCROLL]
    sub    x11, x0, x10
    mov    x12, #ROWH
    mul    x11, x11, x12
    ldr    x13, [x9, #UI_TOPY]
    add    x0, x11, x13
    ret
    .size nota_ui_row_y, . - nota_ui_row_y

/* ========================================================================= *
 * long nota_ui_hit_button(long x, long y) - 1 if the new-note button was hit.
 * ========================================================================= */
    .globl nota_ui_hit_button
    .type  nota_ui_hit_button, %function
nota_ui_hit_button:
    adrp   x9, nota_ui
    add    x9, x9, :lo12:nota_ui
    ldr    x10, [x9, #UI_BTN_X]
    cmp    x0, x10
    b.lo   .Lhb_no
    ldr    x11, [x9, #UI_BTN_S]
    add    x11, x10, x11
    cmp    x0, x11
    b.hs   .Lhb_no
    ldr    x10, [x9, #UI_BTN_Y]
    cmp    x1, x10
    b.lo   .Lhb_no
    ldr    x11, [x9, #UI_BTN_S]
    add    x11, x10, x11
    cmp    x1, x11
    b.hs   .Lhb_no
    mov    x0, #1
    ret
.Lhb_no:
    mov    x0, #0
    ret
    .size nota_ui_hit_button, . - nota_ui_hit_button

/* ========================================================================= *
 * void nota_ui_solid(long x, long y, long w, long h)
 * Solid rectangle in the current colour, sampling the atlas's white cell so
 * text and chrome share one texture and one draw call.
 * ========================================================================= */
    .globl nota_ui_solid
    .type  nota_ui_solid, %function
nota_ui_solid:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    mov    x19, x0
    mov    x20, x1
    mov    x21, x2
    mov    x22, x3
    /* The white cell's centre in texels, computed once. */
    adrp   x9, nota_font_white
    add    x9, x9, :lo12:nota_font_white
    ldr    s0, [x9]
    ldr    s1, [x9, #4]
    mov    w10, #208
    scvtf  s2, w10
    fmul   s0, s0, s2
    mov    w10, #320
    scvtf  s2, w10
    fmul   s1, s1, s2
    fcvtas w4, s0
    fcvtas w5, s1
    mov    w6, w4
    mov    w7, w5
    mov    x0, x19
    mov    x1, x20
    add    x2, x19, x21
    add    x3, x20, x22
    bl     nota_gl_quad
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_ui_solid, . - nota_ui_solid

/* ========================================================================= *
 * void nota_ui_fill(long x, long y, long w, long h, long rgba)
 * Set the colour then draw - the common case.
 * ========================================================================= */
    .globl nota_ui_fill
    .type  nota_ui_fill, %function
nota_ui_fill:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    mov    x19, x0
    mov    x20, x1
    mov    x21, x2
    mov    x22, x3
    mov    w0, w4
    bl     nota_gl_color0
    mov    x0, x19
    mov    x1, x20
    mov    x2, x21
    mov    x3, x22
    bl     nota_ui_solid
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_ui_fill, . - nota_ui_fill

/* ========================================================================= *
 * void nota_ui_round_rect(long x, long y, long w, long h, long r, long rgba)
 * Body plus two side bands plus four scanline-filled quarter discs. r <= 1
 * degenerates to a plain rect.
 * ========================================================================= */
    .globl nota_ui_round_rect
    .type  nota_ui_round_rect, %function
nota_ui_round_rect:
    /* frame: locals at 72 and 80 sit above the saved x25 slot at 64 */
    stp    x29, x30, [sp, #-96]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    str    x25, [sp, #64]
    mov    x19, x0              /* x */
    mov    x20, x1              /* y */
    mov    x21, x2              /* w */
    mov    x22, x3              /* h */
    mov    x23, x4              /* r */
    mov    x24, x5              /* colour */
    cmp    x23, #2
    b.lo   .Lrr_plain

    /* middle band: full width between the caps */
    mov    x0, x19
    add    x1, x20, x23
    mov    x2, x21
    sub    x3, x22, x23
    sub    x3, x3, x23
    mov    w4, w24
    cmp    x3, #0
    b.le   .Lrr_bands
    bl     nota_ui_fill
.Lrr_bands:
    /* top and bottom bands, inset by the radius on both sides */
    mov    x0, x19
    add    x0, x0, x23
    mov    x1, x20
    sub    x2, x21, x23
    sub    x2, x2, x23
    mov    x3, x23
    mov    w4, w24
    bl     nota_ui_fill
    mov    x0, x19
    add    x0, x0, x23
    add    x1, x20, x22
    sub    x1, x1, x23
    sub    x2, x21, x23
    sub    x2, x2, x23
    mov    x3, x23
    mov    w4, w24
    bl     nota_ui_fill

    /* corner scanlines: step 2px, which reads as a smooth curve at the sizes
       the UI uses and halves the quad count */
    mov    x25, #0
.Lrr_scan:
    cmp    x25, x23
    b.hs   .Lrr_done
    /* dy measured from the flat edge: d = r - y, inset = r - sqrt(r^2-d^2) */
    sub    x9, x23, x25
    mul    x10, x9, x9
    mul    x11, x23, x23
    sub    x0, x11, x10
    cmp    x0, #0
    b.le   .Lrr_next
    bl     nota_isqrt
    mov    x9, x23
    sub    x9, x9, x0           /* full inset from the corner */
    /* split into the left and right pieces of this scanline */
    sub    x10, x23, x9         /* left piece width */
    str    x10, [sp, #72]
    /* The inset needs to survive the fills below, and it cannot share the
       slot above: locals are 8 bytes each, so 72 and 76 overlap. */
    str    x9, [sp, #80]

    /* top-left */
    mov    x0, x19
    add    x0, x0, x9
    add    x1, x20, x25
    mov    x2, x10
    mov    x3, #1
    mov    w4, w24
    bl     nota_ui_fill
    /* top-right */
    mov    x0, x19
    add    x0, x0, x21
    sub    x0, x0, x23
    add    x1, x20, x25
    ldr    x2, [sp, #72]
    mov    x3, #1
    mov    w4, w24
    bl     nota_ui_fill
    /* bottom-left. x9 held the inset but the two fills above clobber it, so
       the inset is kept in a stack slot rather than reused from a register. */
    mov    x0, x19
    ldr    x9, [sp, #80]
    add    x0, x0, x9
    add    x1, x20, x22
    sub    x1, x1, x25
    sub    x1, x1, #1
    ldr    x2, [sp, #72]
    mov    x3, #1
    mov    w4, w24
    bl     nota_ui_fill
    /* bottom-right */
    mov    x0, x19
    add    x0, x0, x21
    sub    x0, x0, x23
    add    x1, x20, x22
    sub    x1, x1, x25
    sub    x1, x1, #1
    ldr    x2, [sp, #72]
    mov    x3, #1
    mov    w4, w24
    bl     nota_ui_fill
.Lrr_next:
    add    x25, x25, #2
    b      .Lrr_scan
.Lrr_plain:
    mov    x0, x19
    mov    x1, x20
    mov    x2, x21
    mov    x3, x22
    mov    w4, w24
    bl     nota_ui_fill
.Lrr_done:
    ldr    x25, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #96
    ret
    .size nota_ui_round_rect, . - nota_ui_round_rect

/* ========================================================================= *
 * long nota_ui_wrap(const char *s, long len, long maxw)
 * Breaks text into lines that fit maxw pixels, storing the *address* of each
 * line start in nota_wrap and the count in nota_wrap_n. Returns the count.
 *
 * Addresses rather than offsets: the editor needs "start and end of line N"
 * and having both already absolute removes an add from every draw and every
 * caret lookup, and there is no second base that could disagree.
 *
 * Breaks after the last space that still fits; a single word wider than the
 * line is broken where it overflowed, so a long URL still makes progress
 * instead of looping or vanishing.
 * ========================================================================= */
    .globl nota_ui_wrap
    .type  nota_ui_wrap, %function
nota_ui_wrap:
    stp    x29, x30, [sp, #-112]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    stp    x27, x28, [sp, #80]
    mov    x19, x0              /* s   */
    add    x20, x0, x1          /* end */
    mov    x21, x2              /* maxw */
    adrp   x9, nota_wrap
    add    x9, x9, :lo12:nota_wrap
    mov    x22, x9              /* line-start write cursor */
    mov    x23, #0              /* line count  */
    mov    x24, x19             /* line start  */

.Lwr_newline:
    str    x24, [x22], #8
    add    x23, x23, #1
    mov    x25, x24             /* scan cursor                    */
    mov    x26, x24             /* offset just past the last space */
    mov    x27, #0              /* width consumed on this line     */
.Lwr_scan:
    cmp    x25, x20
    b.hs   .Lwr_last
    ldrb   w9, [x25]
    cmp    w9, #10
    b.eq   .Lwr_hard
    cmp    w9, #32
    b.eq   .Lwr_space
    mov    x0, x25
    add    x1, sp, #104
    bl     nota_utf8_decode
    cbz    x0, .Lwr_last
    mov    x28, x0              /* bytes in this glyph */
    ldr    w0, [sp, #104]
    bl     nota_glyph
    tbnz   x0, #63, .Lwr_unmapped   /* no glyph: counts as nothing */
    /* nota_glyph returns the index in x0 and nota_glyph_advance takes it in
       x0, so the two calls cannot simply be chained: the second would measure
       whatever the first left behind. That made the measured width unrelated
       to the text, and lines broke in the wrong place. */
    bl     nota_glyph_advance
    add    x11, x27, x0
    cmp    x11, x21
    b.hi   .Lwr_break
    mov    x27, x11
    add    x25, x25, x28
    b      .Lwr_scan
.Lwr_unmapped:
    add    x25, x25, x28
    b      .Lwr_scan
.Lwr_space:
    /* A space is a break opportunity: remember the offset after it. Only
       worth remembering if what precedes it actually fits. */
    add    x9, x25, #1
    add    x10, x27, #4          /* a space advances, approximately */
    cmp    x10, x21
    b.hi   .Lwr_break
    mov    x26, x9
    mov    x27, x10
    add    x25, x25, #1
    b      .Lwr_scan
.Lwr_hard:
    add    x24, x25, #1
    b      .Lwr_newline
.Lwr_break:
    cmp    x26, x24
    b.hi   .Lwr_use_space
    /* no break opportunity on this line: hard-break at the overflow point,
       but never at the line start (that would loop forever) */
    cmp    x25, x24
    b.hi   1f
    add    x25, x24, #1
1:  mov    x24, x25
    b      .Lwr_newline
.Lwr_use_space:
    mov    x24, x26
    b      .Lwr_newline
.Lwr_last:
    adrp   x9, nota_wrap_n
    add    x9, x9, :lo12:nota_wrap_n
    str    x23, [x9]
    mov    x0, x23
    ldp    x27, x28, [sp, #80]
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #112
    ret
    .size nota_ui_wrap, . - nota_ui_wrap

    .section .note.GNU-stack,"",%progbits
