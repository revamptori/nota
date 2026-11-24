/*
 * main.S - the NativeActivity lifecycle, EGL setup and the main loop.
 *
 * NativeActivity loads the library and calls ANativeActivity_onCreate on the
 * Java thread. From there:
 *
 *   onNativeWindowCreated -> build the EGL surface, init GL, start drawing
 *   onNativeWindowRedrawNeeded / onContentRectChanged -> redraw
 *   onInputQueueCreated -> hand the queue to app/glue.c's pump thread
 *   onPause/onStop -> commit and save
 *   onNativeWindowDestroyed / onDestroy -> tear EGL down
 *
 * Drawing happens on the Java thread, driven by the callbacks that Android
 * guarantees are delivered there, so there is no render thread to
 * synchronise with and no frame pacing to get wrong. The event pump runs
 * separately and only posts input.
 */
#include "nota.h"

/* ------------------------------------------------------------- EGL bits --- */
    .equ EGL_OPENGL_ES2_BIT, 4
    .equ EGL_CONTEXT_CLIENT_VERSION, 0x3098
    .equ EGL_SURFACE_TYPE,    0x3033
    .equ EGL_WINDOW_BIT,      0x0004
    .equ EGL_RENDERABLE_TYPE, 0x3040
    .equ EGL_RED_SIZE,        0x3024
    .equ EGL_GREEN_SIZE,      0x3023
    .equ EGL_BLUE_SIZE,       0x3022
    .equ EGL_ALPHA_SIZE,      0x3021
    .equ EGL_DEPTH_SIZE,      0x3025
    .equ EGL_NONE,            0x3038

/* ------------------------------------------------------- app state block -- */
    .equ AS_ACTIVITY,  0x00   /* ANativeActivity*                     */
    .equ AS_WINDOW,    0x08   /* ANativeWindow*                       */
    .equ AS_QUEUE,     0x10   /* AInputQueue*                         */
    .equ AS_EGL_DISPLAY, 0x18
    .equ AS_EGL_SURFACE, 0x20
    .equ AS_EGL_CONTEXT, 0x28
    .equ AS_CONFIG,    0x30   /* EGLConfig                            */
    .equ AS_READY,     0x38   /* long: GL is initialised              */
    .equ AS_WIDTH,     0x40
    .equ AS_HEIGHT,    0x48
    .equ AS_SIZE,      0x50

    .section .bss
    .p2align 4
    .globl nota_as
    .hidden nota_as
    .type  nota_as, %object
nota_as:
    .space AS_SIZE
    .size nota_as, . - nota_as

    .equ EGL_CFG_BYTES, 0x40

    .text

/* ========================================================================= *
 * long nota_egl_init(ANativeWindow *win)
 * Creates the display, picks an ES2 config, makes the surface and context
 * current, then hands off to nota_gl_init. Returns 0 on success, -N naming
 * the failing stage otherwise.
 * ========================================================================= */
    .globl nota_egl_init
    .type  nota_egl_init, %function
nota_egl_init:
    stp    x29, x30, [sp, #-192]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    str    x27, [sp, #80]
    mov    x19, x0
    adrp   x20, nota_as
    add    x20, x20, :lo12:nota_as
    str    x19, [x20, #AS_WINDOW]

    /* ---- display: default display, initialised in place ---- */
    mov    w0, #0
    bl     eglGetDisplay
    cbz    x0, .Legl_display
    mov    x21, x0
    str    x21, [x20, #AS_EGL_DISPLAY]
    mov    x0, x21
    add    x1, sp, #160          /* major */
    add    x2, sp, #164          /* minor */
    bl     eglInitialize
    cbz    w0, .Legl_display

    /* ---- config: ES2, 8/8/8/8 RGBA, no depth (the UI is flat) ---- */
    mov    w0, #EGL_SURFACE_TYPE
    str    w0, [sp, #96]
    mov    w0, #EGL_WINDOW_BIT
    str    w0, [sp, #100]
    mov    w0, #EGL_RENDERABLE_TYPE
    str    w0, [sp, #104]
    mov    w0, #EGL_OPENGL_ES2_BIT
    str    w0, [sp, #108]
    mov    w0, #EGL_RED_SIZE
    str    w0, [sp, #112]
    mov    w0, #8
    str    w0, [sp, #116]
    mov    w0, #EGL_GREEN_SIZE
    str    w0, [sp, #120]
    mov    w0, #8
    str    w0, [sp, #124]
    /* The attribute list continues below the saved registers; the frame is
       big enough for all of it. */
    mov    w0, #EGL_BLUE_SIZE
    str    w0, [sp, #128]
    mov    w0, #8
    str    w0, [sp, #132]
    mov    w0, #EGL_ALPHA_SIZE
    str    w0, [sp, #136]
    mov    w0, #8
    str    w0, [sp, #140]
    mov    w0, #EGL_DEPTH_SIZE
    str    w0, [sp, #144]
    mov    w0, #0
    str    w0, [sp, #148]
    mov    w0, #EGL_NONE
    str    w0, [sp, #152]

    mov    x0, x21
    add    x1, sp, #96
    add    x2, sp, #152          /* &config */
    mov    w3, #1
    add    x4, sp, #156          /* &num_config */
    bl     eglChooseConfig
    cbz    w0, .Legl_config
    ldr    x22, [sp, #152]
    str    x22, [x20, #AS_CONFIG]

    /* ---- surface ---- */
    mov    x0, x21
    mov    x1, x22
    mov    x2, x19
    mov    x3, #0
    bl     eglCreateWindowSurface
    cbz    x0, .Legl_surface
    mov    x23, x0
    str    x23, [x20, #AS_EGL_SURFACE]

    /* ---- context ---- */
    mov    w0, #EGL_CONTEXT_CLIENT_VERSION
    str    w0, [sp, #96]
    mov    w0, #2
    str    w0, [sp, #100]
    mov    w0, #EGL_NONE
    str    w0, [sp, #104]
    mov    x0, x21
    mov    x1, x22
    mov    x2, #0
    add    x3, sp, #96
    bl     eglCreateContext
    cbz    x0, .Legl_context
    mov    x24, x0
    str    x24, [x20, #AS_EGL_CONTEXT]

    mov    x0, x21
    mov    x1, x23
    mov    x2, x23
    mov    x3, x24
    bl     eglMakeCurrent
    cbz    w0, .Legl_make

    /* ---- renderer ---- */
    bl     nota_gl_init
    cbnz   x0, .Legl_gl
    mov    x9, #1
    str    x9, [x20, #AS_READY]

    /* nothing animates, so a swap interval of 1 just adds latency */
    mov    x0, x21
    mov    x1, x23
    mov    w2, #0
    bl     eglSwapInterval
    mov    x0, #0
    b      .Legl_out
.Legl_display: mov    x0, #-1
               b      .Legl_out
.Legl_config:  mov    x0, #-2
               b      .Legl_out
.Legl_surface: mov    x0, #-3
               b      .Legl_out
.Legl_context: mov    x0, #-4
               b      .Legl_out
.Legl_make:    mov    x0, #-5
               b      .Legl_out
.Legl_gl:      mov    x0, #-6
.Legl_out:
    ldr    x27, [sp, #80]
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #192
    ret
    .size nota_egl_init, . - nota_egl_init

/* ========================================================================= *
 * void nota_egl_term(void) - drop everything EGL, in reverse order.
 * ========================================================================= */
    .globl nota_egl_term
    .type  nota_egl_term, %function
nota_egl_term:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    adrp   x19, nota_as
    add    x19, x19, :lo12:nota_as
    ldr    x9, [x19, #AS_READY]
    cbz    x9, .Lterm_out
    ldr    x20, [x19, #AS_EGL_DISPLAY]

    mov    x0, x20
    mov    x1, #0
    mov    x2, #0
    mov    x3, #0
    bl     eglMakeCurrent

    ldr    x0, [x19, #AS_EGL_CONTEXT]
    cbz    x0, 1f
    mov    x1, x20
    bl     eglDestroyContext
1:  ldr    x0, [x19, #AS_EGL_SURFACE]
    cbz    x0, 2f
    mov    x1, x20
    bl     eglDestroySurface
2:  ldr    x9, [x19, #AS_READY]
    str    xzr, [x19, #AS_READY]
    cbz    x20, .Lterm_out
    mov    x0, x20
    bl     eglTerminate
.Lterm_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_egl_term, . - nota_egl_term

/* ========================================================================= *
 * void nota_redraw(void)
 * One frame, end to end: measure, lay out, paint, present.
 * ========================================================================= */
    .globl nota_redraw
    .type  nota_redraw, %function
nota_redraw:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    adrp   x19, nota_as
    add    x19, x19, :lo12:nota_as
    ldr    x9, [x19, #AS_READY]
    cbz    x9, .Lrd_out
    ldr    x20, [x19, #AS_WINDOW]
    cbz    x20, .Lrd_out

    /* Ask the window, not a cached size: this is what makes rotation work
       without any resize plumbing. */
    mov    x0, x20
    bl     ANativeWindow_getWidth
    mov    x21, x0
    mov    x0, x20
    bl     ANativeWindow_getHeight
    mov    x20, x0
    str    x21, [x19, #AS_WIDTH]
    str    x20, [x19, #AS_HEIGHT]

    mov    x0, x21
    mov    x1, x20
    bl     nota_ui_layout
    mov    x0, x21
    mov    x1, x20
    bl     nota_gl_frame

    bl     nota_ui_screen
    cbz    x0, 1f
    bl     nota_screen_edit
    b      2f
1:  bl     nota_screen_list
2:  bl     nota_gl_flush

    ldr    x0, [x19, #AS_EGL_DISPLAY]
    ldr    x1, [x19, #AS_EGL_SURFACE]
    bl     eglSwapBuffers
.Lrd_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_redraw, . - nota_redraw

    .section .note.GNU-stack,"",%progbits
