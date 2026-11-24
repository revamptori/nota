/*
 * lifecycle.S - ANativeActivity_onCreate, the callback table and app_glue.
 *
 * NativeActivity dlopen()s libnota.so and calls ANativeActivity_onCreate.
 * Everything else arrives through the callbacks table, which is a static
 * array of function pointers published on the ANativeActivity we are handed.
 *
 * The app glue (android_native_app_glue) is normally linked in as a static
 * library; NOTA has no way to obtain it without the NDK, so the small part of
 * it that matters - a struct android_app and the two handler pointers - is
 * built here. app/glue.c pumps input through nota_app_input.
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
    .equ AS_SIZE,      0x50

/* ========================================================================= *
 * void ANativeActivity_onCreate(ANativeActivity *activity, void *saved,
 *                               size_t saved_size)
 * The library's entry point. Publishes the callback table and initialises the
 * model; EGL waits for a window.
 * ========================================================================= */
    .globl ANativeActivity_onCreate
    .type  ANativeActivity_onCreate, %function
ANativeActivity_onCreate:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    mov    x19, x0
    cbz    x19, .Llc_out

    /* activity->callbacks = &nota_callbacks */
    adrp   x9, nota_callbacks
    add    x9, x9, :lo12:nota_callbacks
    str    x9, [x19, #NA_CALLBACKS]

    adrp   x20, nota_as
    add    x20, x20, :lo12:nota_as
    /* clear it first: a stale AS_READY after a process restart would make the
       first redraw call into a GL context that does not exist */
    stp    xzr, xzr, [x20]
    stp    xzr, xzr, [x20, #16]
    stp    xzr, xzr, [x20, #32]
    stp    xzr, xzr, [x20, #48]
    str    x19, [x20, #AS_ACTIVITY]

    bl     nota_app_init

    /* The app glue struct is static: exactly one activity exists per
       process, and a heap allocation here would only add a failure mode. */
    adrp   x21, nota_glue_app
    add    x21, x21, :lo12:nota_glue_app
    stp    xzr, xzr, [x21]
    stp    xzr, xzr, [x21, #16]
    stp    xzr, xzr, [x21, #32]
    stp    xzr, xzr, [x21, #48]
    stp    xzr, xzr, [x21, #64]
    stp    xzr, xzr, [x21, #80]
    str    x19, [x21, #APP_ACTIVITY]
    /* onInputEvent = nota_app_input; the glue reads this field */
    adrp   x9, nota_app_input
    add    x9, x9, :lo12:nota_app_input
    str    x9, [x21, #APP_ON_INPUT]
    /* looper for the pump thread */
    mov    w0, #0
    bl     ALooper_prepare
    str    x0, [x21, #APP_LOOPER]
    mov    x9, #1
    str    x9, [x21, #APP_RUNNING]
    str    x21, [x20, #0x50]    /* keep the glue struct reachable */

.Llc_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size ANativeActivity_onCreate, . - ANativeActivity_onCreate

/* ========================================================================= *
 * callbacks
 * ========================================================================= */

    .globl nota_cb_window_created
    .type  nota_cb_window_created, %function
nota_cb_window_created:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    str    x19, [sp, #16]
    /* x1 is the ANativeWindow*; the activity pointer in x0 is not needed */
    mov    x0, x1
    bl     nota_egl_init
    cbz    x0, 1f
    /* No GL: the app cannot draw anything, but it can still save. Record the
       failure rather than looping on a broken context. */
    adrp   x9, nota_as
    add    x9, x9, :lo12:nota_as
    str    x0, [x9, #AS_READY]  /* negative: "tried and failed" */
    b      2f
1:  bl     nota_redraw
2:  ldr    x19, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_cb_window_created, . - nota_cb_window_created

    .globl nota_cb_window_redraw
    .type  nota_cb_window_redraw, @function
nota_cb_window_redraw:
    stp    x29, x30, [sp, #-16]!
    mov    x29, sp
    bl     nota_redraw
    ldp    x29, x30, [sp], #16
    ret
    .size nota_cb_window_redraw, . - nota_cb_window_redraw

    .globl nota_cb_window_resized
    .type  nota_cb_window_resized, %function
nota_cb_window_resized:
    /* The size is read from the window every frame, so a resize only needs a
       repaint; there is nothing to invalidate. */
    b      nota_cb_window_redraw
    .size nota_cb_window_resized, . - nota_cb_window_resized

    .globl nota_cb_window_destroyed
    .type  nota_cb_window_destroyed, %function
nota_cb_window_destroyed:
    stp    x29, x30, [sp, #-16]!
    mov    x29, sp
    bl     nota_egl_term
    adrp   x9, nota_as
    add    x9, x9, :lo12:nota_as
    str    xzr, [x9, #AS_WINDOW]
    ldp    x29, x30, [sp], #16
    ret
    .size nota_cb_window_destroyed, . - nota_cb_window_destroyed

    .globl nota_cb_queue_created
    .type  nota_cb_queue_created, %function
nota_cb_queue_created:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    str    x19, [sp, #16]
    adrp   x9, nota_as
    add    x9, x9, :lo12:nota_as
    str    x1, [x9, #AS_QUEUE]
    /* The glue struct is a known object, not something to recover from a
       clobbered argument: onInputQueueCreated is called with x0 = activity
       and x1 = queue. */
    adrp   x19, nota_glue_app
    add    x19, x19, :lo12:nota_glue_app
    cbz    x19, 1f
    str    x1, [x19, #APP_INPUT_QUEUE]
    str    x1, [x19, #APP_PEND_QUEUE]
    mov    x0, x19
    bl     nota_glue_start_input
1:  ldr    x19, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_cb_queue_created, . - nota_cb_queue_created

    .globl nota_cb_queue_destroyed
    .type  nota_cb_queue_destroyed, %function
nota_cb_queue_destroyed:
    adrp   x9, nota_as
    add    x9, x9, :lo12:nota_as
    str    xzr, [x9, #AS_QUEUE]
    ldr    x9, [x9, #0x50]
    cbz    x9, 1f
    str    xzr, [x9, #APP_INPUT_QUEUE]
    str    xzr, [x9, #APP_PEND_QUEUE]
1:  ret
    .size nota_cb_queue_destroyed, . - nota_cb_queue_destroyed

    .globl nota_cb_pause
    .type  nota_cb_pause, %function
nota_cb_pause:
    stp    x29, x30, [sp, #-16]!
    mov    x29, sp
    /* Committing on pause means losing the process cannot lose text: fold the
       editor's buffer into the note, then push the collection to disk. Pause
       is the last point Android guarantees to call, so the save belongs here
       rather than in destroy, which a process kill can skip entirely. */
    bl     nota_note_commit
    bl     nota_app_save
    ldp    x29, x30, [sp], #16
    ret
    .size nota_cb_pause, . - nota_cb_pause

    .globl nota_cb_stop
    .type  nota_cb_stop, %function
nota_cb_stop:
    b      nota_cb_pause
    .size nota_cb_stop, . - nota_cb_stop

    .globl nota_cb_destroy
    .type  nota_cb_destroy, %function
nota_cb_destroy:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    str    x19, [sp, #16]
    /* Normally a no-op: pause already saved and cleared the dirty flag. It
       still matters when destroy is reached without an intervening pause. */
    bl     nota_app_save
    adrp   x9, nota_as
    add    x9, x9, :lo12:nota_as
    ldr    x19, [x9, #0x50]
    cbz    x19, 1f
    str    xzr, [x19, #APP_RUNNING]
    mov    x10, #1
    str    x10, [x19, #APP_DESTROYED]
1:  bl     nota_egl_term
    ldr    x19, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_cb_destroy, . - nota_cb_destroy

    .globl nota_cb_focus
    .type  nota_cb_focus, %function
nota_cb_focus:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    str    x19, [sp, #16]
    mov    w19, w1
    cbz    w19, 1f
    /* Focused: show the keyboard, which is what makes the editor usable. */
    ldr    x0, [x0]
    adrp   x9, nota_as
    add    x9, x9, :lo12:nota_as
    ldr    x0, [x9, #AS_ACTIVITY]
    cbz    x0, 2f
    mov    w1, #0
    bl     ANativeActivity_showSoftInput
    b      2f
1:  mov    x19, x0
    ldr    x0, [x19]
    adrp   x9, nota_as
    add    x9, x9, :lo12:nota_as
    ldr    x0, [x9, #AS_ACTIVITY]
    cbz    x0, 2f
    mov    w1, #0
    bl     ANativeActivity_hideSoftInput
2:  bl     nota_redraw
    ldr    x19, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_cb_focus, . - nota_cb_focus

    .globl nota_cb_content_rect
    .type  nota_cb_content_rect, %function
nota_cb_content_rect:
    b      nota_cb_window_redraw
    .size nota_cb_content_rect, . - nota_cb_content_rect

    .globl nota_cb_config
    .type  nota_cb_config, %function
nota_cb_config:
    b      nota_cb_window_redraw
    .size nota_cb_config, . - nota_cb_config

    .globl nota_cb_low_memory
    .type  nota_cb_low_memory, %function
nota_cb_low_memory:
    /* The undo arena is the only reclaimable memory. Dropping it costs the
       user their undo stack, which beats being killed mid-edit. */
    stp    x29, x30, [sp, #-16]!
    mov    x29, sp
    adrp   x0, nota_doc
    add    x0, x0, :lo12:nota_doc
    bl     nota_undo_clear
    ldp    x29, x30, [sp], #16
    ret
    .size nota_cb_low_memory, . - nota_cb_low_memory

    .globl nota_cb_noop
    .type  nota_cb_noop, %function
nota_cb_noop:
    ret
    .size nota_cb_noop, . - nota_cb_noop

/* ========================================================================= *
 * The table. Order must match ANativeActivityCallbacks in nota.h.
 * ========================================================================= */
    .section .bss
    .p2align 4
    .globl nota_glue_app
    .hidden nota_glue_app
    .type  nota_glue_app, %object
nota_glue_app:
    .space APP_SIZE
    .size nota_glue_app, . - nota_glue_app

    .section .data.rel.ro
    .p2align 3
    .globl nota_callbacks
    .hidden nota_callbacks
    .type  nota_callbacks, %object
nota_callbacks:
    .quad nota_cb_noop            /* 0x00 onStart                 */
    .quad nota_cb_noop            /* 0x08 onResume                */
    .quad nota_cb_noop            /* 0x10 onSaveInstanceState     */
    .quad nota_cb_pause           /* 0x18 onPause                 */
    .quad nota_cb_stop            /* 0x20 onStop                  */
    .quad nota_cb_destroy         /* 0x28 onDestroy               */
    .quad nota_cb_focus           /* 0x30 onWindowFocusChanged    */
    .quad nota_cb_window_created  /* 0x38 onNativeWindowCreated   */
    .quad nota_cb_window_resized  /* 0x40 onNativeWindowResized   */
    .quad nota_cb_window_redraw   /* 0x48 onNativeWindowRedrawNeeded */
    .quad nota_cb_window_destroyed /* 0x50 onNativeWindowDestroyed */
    .quad nota_cb_queue_created   /* 0x58 onInputQueueCreated     */
    .quad nota_cb_queue_destroyed /* 0x60 onInputQueueDestroyed   */
    .quad nota_cb_content_rect    /* 0x68 onContentRectChanged    */
    .quad nota_cb_config          /* 0x70 onConfigurationChanged  */
    .quad nota_cb_low_memory      /* 0x78 onLowMemory             */
    .size nota_callbacks, . - nota_callbacks

    .section .note.GNU-stack,"",%progbits
