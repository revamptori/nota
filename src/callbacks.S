/*
 * callbacks.S - ANativeActivity callbacks, touch and key handling.
 *
 * Android delivers lifecycle callbacks on the Java thread; this file turns
 * them into NOTA operations and repaints. Input arrives on the glue's pump
 * thread and is dispatched straight into the editor, then the next redraw
 * shows the result. Because the editor is only touched from one thread at a
 * time in practice (touch events are serialised by the input queue) there is
 * no lock; the pump is the only writer while a frame is being built on the
 * Java thread, which is why a frame reads the document exactly once at the
 * start of painting.
 */
#include "nota.h"

    .equ AS_ACTIVITY,  0x00
    .equ AS_WINDOW,    0x08
    .equ AS_QUEUE,     0x10
    .equ AS_EGL_DISPLAY, 0x18
    .equ AS_EGL_SURFACE, 0x20
    .equ AS_EGL_CONTEXT, 0x28
    .equ AS_CONFIG,    0x30
    .equ AS_READY,     0x38
    .equ AS_WIDTH,     0x40
    .equ AS_HEIGHT,    0x48

    .text

/* ========================================================================= *
 * void nota_app_init(void)
 * ========================================================================= */
    .globl nota_app_init
    .type  nota_app_init, %function
nota_app_init:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    /* Init first: load decodes records into the collection, so the store,
       the note array and the arena must already be set up. */
    bl     nota_notes_init
    bl     nota_app_load
    ldp    x29, x30, [sp], #32
    ret
    .size nota_app_init, . - nota_app_init

/* ========================================================================= *
 * long nota_app_input(android_app *app, AInputEvent *event)
 * Called from the glue's pump thread for every queued event.
 * Returns 1 if consumed.
 * ========================================================================= */
    .globl nota_app_input
    .hidden nota_app_input
    .type  nota_app_input, %function
nota_app_input:
    stp    x29, x30, [sp, #-96]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    str    x27, [sp, #80]
    mov    x19, x0
    mov    x20, x1

    mov    x0, x20
    bl     AInputEvent_getType
    cmp    w0, #AINPUT_EVENT_TYPE_KEY
    b.eq   .Lai_key
    cmp    w0, #AINPUT_EVENT_TYPE_MOTION
    b.eq   .Lai_touch
    mov    x0, #0
    b      .Lai_out

/* ------------------------------------------------------------------ keys -- */
.Lai_key:
    mov    x0, x20
    bl     AKeyEvent_getAction
    cbnz   w0, .Lai_consumed    /* only down events act; up is swallowed */
    mov    x0, x20
    bl     AKeyEvent_getKeyCode
    mov    x21, x0
    mov    x0, x20
    bl     AKeyEvent_getMetaState
    mov    x22, x0

    /* Back leaves the editor, or exits from the list. */
    mov    w9, #AKEYCODE_BACK
    cmp    x21, x9
    b.ne   .Lai_editor_keys
    bl     nota_ui_screen
    cbz    x0, .Lai_exit
    bl     nota_note_close
    bl     nota_redraw
    b      .Lai_consumed
.Lai_exit:
    ldr    x9, [x19, #APP_ACTIVITY]
    cbz    x9, .Lai_consumed
    mov    x0, x9
    bl     ANativeActivity_finish
    b      .Lai_consumed
    b      .Lai_consumed

.Lai_editor_keys:
    bl     nota_ui_screen
    cbz    x0, .Lai_consumed    /* list has no keyboard actions */

    mov    w9, #AKEYCODE_FORWARD_DEL
    cmp    x21, x9
    b.ne   1f
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    bl     nota_doc_delete_forward
    b      .Lai_edited
1:  mov    w9, #AKEYCODE_DEL
    cmp    x21, x9
    b.ne   2f
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    bl     nota_doc_backspace
    b      .Lai_edited
2:  mov    w9, #AKEYCODE_ENTER
    cmp    x21, x9
    b.ne   3f
    adrp   x0, nota_doc
    add   x0, x0, :lo12:nota_doc
    mov    w1, #10
    bl     nota_doc_insert_cp
    b      .Lai_edited
3:  mov    w9, #AKEYCODE_DPAD_LEFT
    cmp    x21, x9
    b.ne   4f
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    tst    w22, #AMETA_CTRL_ON
    cset   w1, ne
    bl     nota_doc_move_left
    b      .Lai_moved
4:  mov    w9, #AKEYCODE_DPAD_RIGHT
    cmp    x21, x9
    b.ne   5f
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    tst    w22, #AMETA_CTRL_ON
    cset   w1, ne
    bl     nota_doc_move_right
    b      .Lai_moved
5:  mov    w9, #AKEYCODE_DPAD_UP
    cmp    x21, x9
    b.ne   6f
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    mov    x1, #-1
    tst    w22, #AMETA_CTRL_ON
    cset   w2, ne
    bl     nota_doc_move_vert
    b      .Lai_moved
6:  mov    w9, #AKEYCODE_DPAD_DOWN
    cmp    x21, x9
    b.ne   7f
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    mov    x1, #1
    tst    w22, #AMETA_CTRL_ON
    cset   w2, ne
    bl     nota_doc_move_vert
    b      .Lai_moved
7:  mov    w9, #AKEYCODE_MOVE_HOME
    cmp    x21, x9
    b.ne   8f
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    tst    w22, #AMETA_CTRL_ON
    cset   w1, ne
    bl     nota_doc_move_line_start
    b      .Lai_moved
8:  mov    w9, #AKEYCODE_MOVE_END
    cmp    x21, x9
    b.ne   9f
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    tst    w22, #AMETA_CTRL_ON
    cset   w1, ne
    bl     nota_doc_move_line_end
    b      .Lai_moved
9:  /* printable A-Z, space and the digit row; a soft keyboard delivers these
       as key events with a UNICODE_CHAR, which ES2-era NDK does not expose,
       so the keycode is mapped directly and non-Latin input is out of scope
       for a keyboard that only ever shows the ASCII layout */
    mov    w9, #AKEYCODE_SPACE
    cmp    x21, x9
    b.ne   .Lai_letters
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    mov    w1, #32
    bl     nota_doc_insert_cp
    b      .Lai_edited
.Lai_letters:
    /* A..Z is 29..54, contiguous; shift decides the case */
    mov    w9, #AKEYCODE_A
    subs   w10, w21, w9
    b.lo   .Lai_digits
    cmp    w10, #25
    b.hi   .Lai_digits
    add    w11, w10, #'a'
    tst    w22, #AMETA_SHIFT_ON
    b.eq   1f
    add    w11, w10, #'A'
1:  adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    mov    w1, w11
    bl     nota_doc_insert_cp
    b      .Lai_edited
.Lai_digits:
    /* 0..9 is 7..16; the shifted symbols are not mapped, which is honest:
       pressing shift+2 on a phone number pad should give '2' */
    mov    w9, #AKEYCODE_0
    subs   w10, w21, w9
    b.lo   .Lai_consumed
    cmp    w10, #9
    b.hi   .Lai_consumed
    add    w11, w10, #'0'
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    mov    w1, w11
    bl     nota_doc_insert_cp
.Lai_edited:
    adrp   x9, nota_app
    add    x9, x9, :lo12:nota_app
    mov    x10, #1
    str    x10, [x9, #0x10]     /* SC_DIRTY */
.Lai_moved:
    bl     nota_redraw
.Lai_consumed:
    mov    x0, #1
    b      .Lai_out

/* ----------------------------------------------------------------- touch -- */
.Lai_touch:
    mov    x0, x20
    bl     AMotionEvent_getAction
    and    w21, w0, #AMOTION_EVENT_ACTION_MASK
    cmp    w21, #AMOTION_EVENT_ACTION_DOWN
    b.eq   .Lai_down
    cmp    w21, #AMOTION_EVENT_ACTION_UP
    b.eq   .Lai_up
    mov    x0, #0
    b      .Lai_out

.Lai_down:
    mov    x0, x20
    mov    x1, #0
    bl     AMotionEvent_getX
    bl     nota_f2i
    mov    x23, x0              /* x */
    mov    x0, x20
    mov    x1, #0
    bl     AMotionEvent_getY
    bl     nota_f2i
    mov    x24, x0              /* y */

    bl     nota_ui_screen
    cbz    x0, .Lai_down_list

    /* --- editor: tap places the caret --- */
    mov    x0, x23
    mov    x1, x24
    bl     nota_edit_caret_at
    b      .Lai_moved
.Lai_down_list:
    /* the button first: it overlaps the bottom rows */
    mov    x0, x23
    mov    x1, x24
    bl     nota_ui_hit_button
    cbnz   x0, .Lai_new_note
    mov    x0, x24
    bl     nota_ui_row_at
    tbnz   x0, #63, .Lai_moved
    mov    x21, x0
    bl     nota_note_open
    bl     nota_redraw
    b      .Lai_consumed
.Lai_new_note:
    bl     nota_note_new
    cbz    x0, .Lai_consumed
    bl     nota_note_count
    sub    x0, x0, #1
    bl     nota_note_open
    bl     nota_redraw
    b      .Lai_consumed

.Lai_up:
    mov    x0, #0
    b      .Lai_out

.Lai_out:
    ldr    x27, [sp, #80]
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #96
    ret
    .size nota_app_input, . - nota_app_input

/* ========================================================================= *
 * long nota_f2i(float f) - truncating float->int, for touch coordinates.
 * ========================================================================= */
    .globl nota_f2i
    .type  nota_f2i, %function
nota_f2i:
    fcvtzs x0, s0
    ret
    .size nota_f2i, . - nota_f2i

/* ========================================================================= *
 * long nota_edit_caret_at(long x, long y)
 * Map a tap in the editor to a byte offset in the document: find the line by
 * y, then walk that line's text accumulating advances until x is passed.
 * ========================================================================= */
    .globl nota_edit_caret_at
    .type  nota_edit_caret_at, %function
nota_edit_caret_at:
    stp    x29, x30, [sp, #-96]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    str    x27, [sp, #80]
    mov    x19, x0              /* tap x */
    mov    x20, x1              /* tap y */

    adrp   x21, nota_doc
    add    x21, x21, :lo12:nota_doc
    adrp   x22, nota_ui
    add    x22, x22, :lo12:nota_ui
    mov    x24, #0x58
    ldr    x25, [x22, x24]           /* UI_EDIT_TOP */
    add    x24, x24, #0x10
    ldr    x26, [x22, x24]           /* UI_LINEH */

    /* which wrapped line? lines start at UI_EDIT_TOP - line height */
    mov    x9, x25
    mov    x10, x26
    sub    x9, x9, #20
    sub    x11, x20, x9
    cmp    x11, #0
    b.le   .Leca_first
    udiv   x23, x11, x10            /* line index */
    b      .Leca_have_line
.Leca_first:
    mov    x23, #0
.Leca_have_line:
    adrp   x9, nota_wrap_n
    add    x9, x9, :lo12:nota_wrap_n
    ldr    x24, [x9]                /* line count */
    cmp    x23, x24
    b.lo   1f
    sub    x23, x24, #1
    cmp    x23, #0
    b.ge   1f
    mov    x23, #0
1:  adrp   x9, nota_wrap
    add    x9, x9, :lo12:nota_wrap
    ldr    x25, [x9, x23, lsl #3]   /* line start offset */

    /* walk to the glyph whose span contains the tap */
    ldr    x26, [x21, #DOC_LEN]     /* 0x10 */
    sub    x27, x19, #20            /* EDIT_PADX */
    ldr    x0, [x21, #DOC_BUF]
    add    x0, x0, x25
    sub    x1, x26, x25
    mov    x2, x27
    bl     nota_edit_offset_at_x
    /* x0 is an offset within the line */
    add    x0, x0, x25
    adrp   x9, nota_doc
    add    x9, x9, :lo12:nota_doc
    mov    x1, x0
    mov    x2, #0
    mov    x0, x9
    bl     nota_doc_move

    ldr    x27, [sp, #80]
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #96
    ret
    .size nota_edit_caret_at, . - nota_edit_caret_at

    .section .note.GNU-stack,"",%progbits
