/*
 * tex.S - the renderer: one shader program, one texture, one vertex buffer.
 *
 * The UI is a stream of axis-aligned quads. Every quad - a glyph, a panel, the
 * caret, a selection band - is six vertices of (x, y, u, v, r, g, b, a) and
 * goes into one buffer flushed once per frame, so a screen full of text costs
 * a single draw call.
 *
 * The projection is rebuilt from the surface size every frame, which is why
 * nothing else handles resize: rotation falls out of asking the window how
 * big it is.
 *
 * The atlas is uploaded as GL_LUMINANCE_ALPHA (luminance 255 everywhere,
 * coverage in alpha) so glyphs are 2 bytes per texel and the fragment shader
 * can use coverage purely as alpha:
 *
 *     gl_FragColor = vec4(v_col.rgb, v_col.a * texture.a);
 */
#include "nota.h"

    .equ NVBUF, GL_MAX_VERTS * GL_VSTRIDE

/* ------------------------------------------------------------------ BSS --- */
    .bss
    .p2align 4
    .globl nota_gl
    .hidden nota_gl
    .type  nota_gl, %object
nota_gl:
    .space GL_STATE_SIZE
    .size nota_gl, . - nota_gl

    .p2align 4
    .globl nota_gl_vbuf
    .hidden nota_gl_vbuf
    .type  nota_gl_vbuf, %object
nota_gl_vbuf:
    .space NVBUF
    .size nota_gl_vbuf, . - nota_gl_vbuf

    /* Luminance-alpha staging copy of the atlas, built once at init. */
    .p2align 4
nota_gl_tex:
    .space 208 * 320 * 2
    .size nota_gl_tex, . - nota_gl_tex

/* --------------------------------------------------------------- shaders --- */
    .section .rodata
    .p2align 3
.Lvs_src:
    .asciz "attribute vec2 a_pos;\nattribute vec2 a_uv;\nattribute vec4 a_col;\nuniform vec2 u_proj;\nvarying vec2 v_uv;\nvarying vec4 v_col;\nvoid main(){\n  vec2 n = a_pos / u_proj;\n  gl_Position = vec4(n.x*2.0-1.0, 1.0-n.y*2.0, 0.0, 1.0);\n  v_uv = a_uv;\n  v_col = a_col;\n}\n"
    .p2align 3
.Lfs_src:
    .asciz "precision mediump float;\nvarying vec2 v_uv;\nvarying vec4 v_col;\nuniform sampler2D u_tex;\nvoid main(){\n  gl_FragColor = vec4(v_col.rgb, v_col.a * texture2D(u_tex, v_uv).a);\n}\n"
.La_pos:  .asciz "a_pos"
.La_uv:   .asciz "a_uv"
.La_col:  .asciz "a_col"
.Lu_proj: .asciz "u_proj"
.Lu_tex:  .asciz "u_tex"

    /* Back to code: the strings above are the only data in this file that is
       not in .bss, and forgetting this made the assembler emit every
       following function into .rodata - which links fine and only fails when
       something branches into it. */
    .text

/* ========================================================================= *
 * long nota_gl_init(void)
 * Builds the program, the atlas texture and the vertex buffer. Every name is
 * resolved here so the per-frame path never looks one up.
 * Returns 0, or -1..-4 naming the stage that failed.
 * ========================================================================= */
    .globl nota_gl_init
    .type  nota_gl_init, %function
nota_gl_init:
    /* frame: x29/x30 at 0, x19-x24 at 16..64, scratch at 72 - outside the
       saved pairs, so a GL call writing its out-param cannot clobber a saved
       register. */
    stp    x29, x30, [sp, #-80]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    adrp   x19, nota_gl
    add    x19, x19, :lo12:nota_gl

    /* ---- vertex shader ------------------------------------------------ */
    mov    w0, #GL_VERTEX_SHADER
    bl     glCreateShader
    mov    x20, x0
    cbz    x20, .Lgi_vs
    adr    x9, .Lvs_src
    str    x9, [sp, #72]
    mov    x0, x20
    add    x1, sp, #72
    mov    w2, #1
    bl     glShaderSource
    mov    x0, x20
    bl     glCompileShader

    /* ---- fragment shader ---------------------------------------------- */
    mov    w0, #GL_FRAGMENT_SHADER
    bl     glCreateShader
    mov    x21, x0
    cbz    x21, .Lgi_fs
    adr    x9, .Lfs_src
    str    x9, [sp, #72]
    mov    x0, x21
    add    x1, sp, #72
    mov    w2, #1
    bl     glShaderSource
    mov    x0, x21
    bl     glCompileShader

    /* ---- program ------------------------------------------------------ */
    bl     glCreateProgram
    mov    x22, x0
    cbz    x22, .Lgi_prog
    mov    x0, x22
    mov    x1, x20
    bl     glAttachShader
    mov    x0, x22
    mov    x1, x21
    bl     glAttachShader
    mov    x0, x22
    bl     glLinkProgram

    /* A link failure on a real device is a shader-text bug, and it is fatal
       but silent unless asked for; ask once. */
    mov    x0, x22
    mov    w1, #GL_LINK_STATUS
    add    x2, sp, #72
    bl     glGetProgramiv
    ldr    w9, [sp, #72]
    cbz    w9, .Lgi_link

    mov    x0, x22
    bl     glUseProgram
    str    w22, [x19, #GL_PROG]

    /* ---- attribute and uniform locations ------------------------------ */
    mov    x0, x22
    adr    x1, .La_pos
    bl     glGetAttribLocation
    str    w0, [x19, #GL_A_POS]
    mov    x0, x22
    adr    x1, .La_uv
    bl     glGetAttribLocation
    str    w0, [x19, #GL_A_UV]
    mov    x0, x22
    adr    x1, .La_col
    bl     glGetAttribLocation
    str    w0, [x19, #GL_A_COL]
    mov    x0, x22
    adr    x1, .Lu_proj
    bl     glGetUniformLocation
    str    w0, [x19, #GL_U_PROJ]
    mov    x0, x22
    adr    x1, .Lu_tex
    bl     glGetUniformLocation
    str    w0, [x19, #GL_U_TEX]

    /* ---- atlas -------------------------------------------------------- */
    /* Expand the 8-bit coverage atlas into luminance-alpha: L = 0xFF, A =
       coverage, so the shader can treat .a as opacity directly. */
    adrp   x9, nota_font_atlas
    add    x9, x9, :lo12:nota_font_atlas
    adrp   x10, nota_gl_tex
    add    x10, x10, :lo12:nota_gl_tex
    /* 208 * 320 = 66560: not a single-instruction immediate */
    movz   w11, #(66560 >> 16), lsl #16
    movk   w11, #(66560 & 0xFFFF)
.Lgi_expand:
    ldrb   w12, [x9], #1
    mov    w13, #255
    strb   w13, [x10], #1       /* luminance */
    strb   w12, [x10], #1       /* alpha     */
    subs   x11, x11, #1
    b.ne   .Lgi_expand

    bl     glGenTextures
    str    x0, [x19, #GL_TEX]
    mov    w0, #GL_TEXTURE0
    bl     glActiveTexture
    mov    w0, #GL_TEXTURE_2D
    ldr    w1, [x19, #GL_TEX]
    bl     glBindTexture

    mov    w0, #GL_TEXTURE_2D
    mov    w1, #GL_TEXTURE_MIN_FILTER
    mov    w2, #GL_LINEAR
    bl     glTexParameteri
    mov    w0, #GL_TEXTURE_2D
    mov    w1, #GL_TEXTURE_MAG_FILTER
    mov    w2, #GL_LINEAR
    bl     glTexParameteri
    mov    w0, #GL_TEXTURE_2D
    mov    w1, #GL_TEXTURE_WRAP_S
    mov    w2, #GL_CLAMP_TO_EDGE
    bl     glTexParameteri
    mov    w0, #GL_TEXTURE_2D
    mov    w1, #GL_TEXTURE_WRAP_T
    mov    w2, #GL_CLAMP_TO_EDGE
    bl     glTexParameteri

    mov    w0, #GL_TEXTURE_2D
    mov    w1, #0
    /* The atlas is one 8-bit coverage byte per texel, and the fragment shader
       samples only .a, so the internal format must be GL_ALPHA. Asking for
       GL_LUMINANCE_ALPHA made the driver read two bytes per texel: twice the
       data the buffer holds, and an alpha that never matches the glyph. */
    mov    w2, #GL_ALPHA
    mov    w3, #208
    mov    w4, #320
    mov    w5, #0
    mov    w6, #GL_ALPHA
    mov    w7, #GL_UNSIGNED_BYTE
    adrp   x9, nota_gl_tex
    add    x9, x9, :lo12:nota_gl_tex
    bl     glTexImage2D

    ldr    w0, [x19, #GL_U_TEX]
    mov    w1, #0
    bl     glUniform1i

    /* ---- vertex buffer ------------------------------------------------ */
    bl     glGenBuffers
    str    x0, [x19, #GL_VBO]
    mov    w0, #GL_ARRAY_BUFFER
    ldr    w1, [x19, #GL_VBO]
    bl     glBindBuffer
    mov    w0, #GL_ARRAY_BUFFER
    mov    w1, #NVBUF
    mov    x2, #0
    mov    w3, #GL_STREAM_DRAW
    bl     glBufferData

    /* ---- attributes: two floats pos, two floats uv, four floats colour -- */
    ldr    w0, [x19, #GL_A_POS]
    mov    w1, #2
    mov    w2, #GL_FLOAT
    mov    w3, #0               /* normalised */
    mov    x4, #0               /* offset      */
    mov    w5, #(GL_VSTRIDE * 4)   /* stride is in bytes, not floats */
    mov    x6, #0
    bl     glVertexAttribPointer
    mov    w0, #0
    bl     glEnableVertexAttribArray
    ldr    w0, [x19, #GL_A_POS]
    bl     glEnableVertexAttribArray

    ldr    w0, [x19, #GL_A_UV]
    mov    w1, #2
    mov    w2, #GL_FLOAT
    mov    w3, #0
    mov    x4, #8
    mov    w5, #(GL_VSTRIDE * 4)   /* stride is in bytes, not floats */
    mov    x6, #0
    bl     glVertexAttribPointer
    ldr    w0, [x19, #GL_A_UV]
    bl     glEnableVertexAttribArray

    ldr    w0, [x19, #GL_A_COL]
    mov    w1, #4
    mov    w2, #GL_FLOAT
    mov    w3, #0
    mov    x4, #16
    mov    w5, #(GL_VSTRIDE * 4)   /* stride is in bytes, not floats */
    mov    x6, #0
    bl     glVertexAttribPointer
    ldr    w0, [x19, #GL_A_COL]
    bl     glEnableVertexAttribArray

    /* ---- blending ----------------------------------------------------- */
    mov    w0, #GL_BLEND
    bl     glEnable
    mov    w0, #GL_SRC_ALPHA
    mov    w1, #GL_ONE_MINUS_SRC_ALPHA
    bl     glBlendFunc

    /* default colour: opaque white */
    mov    w9, #0x3f800000
    str    w9, [x19, #GL_CR]
    str    w9, [x19, #GL_CG]
    str    w9, [x19, #GL_CB]
    str    w9, [x19, #GL_CA]

    mov    x0, #0
    b      .Lgi_out
.Lgi_vs:    mov    x0, #-1
            b      .Lgi_out
.Lgi_fs:    mov    x0, #-2
            b      .Lgi_out
.Lgi_prog:  mov    x0, #-3
            b      .Lgi_out
.Lgi_link:  mov    x0, #-4
.Lgi_out:
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #80
    ret
    .size nota_gl_init, . - nota_gl_init

/* ========================================================================= *
 * void nota_gl_color0(unsigned rgba)        - set colour, 0xRRGGBBAA
 * void nota_gl_color4(float r, g, b, a)     - set colour, components
 * Both store straight-alpha floats in the state block; the vertex writer
 * reads them, so a caller sets the colour once and emits many quads.
 * ========================================================================= */
    .globl nota_gl_color0
    .type  nota_gl_color0, %function
nota_gl_color0:
    adrp   x9, nota_gl
    add    x9, x9, :lo12:nota_gl
    /* 0xAARRGGBB in, straight RGBA out: red comes from bits 23:16, not
       31:24 - reading alpha as red is how 0xFF112233 became (1, .07, .13). */
    mov    w10, #255
    scvtf  s7, w10
    ubfx   w11, w0, #16, #8      /* red */
    scvtf  s0, w11
    fdiv   s0, s0, s7
    ubfx   w11, w0, #8, #8       /* green */
    scvtf  s1, w11
    fdiv   s1, s1, s7
    and    w11, w0, #0xff        /* blue */
    scvtf  s2, w11
    fdiv   s2, s2, s7
    ubfx   w11, w0, #24, #8      /* alpha */
    scvtf  s3, w11
    fdiv   s3, s3, s7
    /* Straight (non-premultiplied) colour: GL_SRC_ALPHA already multiplies
       the source by alpha in the blend stage, so premultiplying here would
       apply it twice and darken every translucent panel. */
    str    s0, [x9, #GL_CR]
    str    s1, [x9, #GL_CG]
    str    s2, [x9, #GL_CB]
    str    s3, [x9, #GL_CA]
    ret
    .size nota_gl_color0, . - nota_gl_color0

    .globl nota_gl_color4
    .type  nota_gl_color4, %function
nota_gl_color4:
    adrp   x9, nota_gl
    add    x9, x9, :lo12:nota_gl
    str    s0, [x9, #GL_CR]
    str    s1, [x9, #GL_CG]
    str    s2, [x9, #GL_CB]
    str    s3, [x9, #GL_CA]
    ret
    .size nota_gl_color4, . - nota_gl_color4

/* ========================================================================= *
 * void nota_gl_quad(long x, long y, long w, long h, long u0, long v0,
 *                   long u1, long v1)
 * Appends one quad using the current colour. Coordinates are pixels; UVs are
 * texel coordinates in the atlas, not normalised, because every caller thinks
 * in glyph cells (16x20) and the division happens here, once.
 * A full queue drops the quad: a dropped decorative pixel beats a corrupt
 * frame, and the buffer is sized so this does not happen on real screens.
 * ========================================================================= */
    .globl nota_gl_quad
    .type  nota_gl_quad, %function
nota_gl_quad:
    adrp   x9, nota_gl
    add    x9, x9, :lo12:nota_gl
    ldr    w10, [x9, #GL_NVERTS]
    add    w11, w10, #6
    cmp    w11, #GL_MAX_VERTS
    b.hi   .Lgq_full
    str    w11, [x9, #GL_NVERTS]

    /* p = vbuf + nverts * stride */
    mov    w12, #GL_VSTRIDE
    madd   w13, w10, w12, wzr
    adrp   x14, nota_gl_vbuf
    add    x14, x14, :lo12:nota_gl_vbuf
    add    x14, x14, x13

    /* Normalise the UV corners once: inv = 1/208, 1/320. */
    ldr    s0, [x9, #GL_CR]
    ldr    s1, [x9, #GL_CG]
    ldr    s2, [x9, #GL_CB]
    ldr    s3, [x9, #GL_CA]
    adrp   x15, .Linv
    add    x15, x15, :lo12:.Linv
    ldr    s8, [x15]            /* 1/208 */
    ldr    s9, [x15, #4]        /* 1/320 */

    scvtf  s4, x4               /* u0 */
    scvtf  s5, x5               /* v0 */
    scvtf  s6, x6               /* u1 */
    scvtf  s7, x7               /* v1 */
    fmul   s4, s4, s8
    fmul   s5, s5, s9
    fmul   s6, s6, s8
    fmul   s7, s7, s9

    /* six vertices: (x0,y0) (x1,y0) (x1,y1) (x0,y0) (x1,y1) (x0,y1) */
    /* Emitting a vertex is: two int->float for pos, two uv, four colour. The
       macro keeps the six repeats honest. */
    .macro VERT gofs, u, v, areg
    scvtf  s10, \areg
    str    s10, [x14], #4
    .endm

    /* --- corner 0 (x0, y0) uv (u0, v0) --- */
    scvtf  s10, x0
    str    s10, [x14], #4
    scvtf  s10, x1
    str    s10, [x14], #4
    str    s4, [x14], #4
    str    s5, [x14], #4
    str    s0, [x14], #4
    str    s1, [x14], #4
    str    s2, [x14], #4
    str    s3, [x14], #4
    /* --- corner 1 (x1, y0) uv (u1, v0) --- */
    scvtf  s10, x2
    str    s10, [x14], #4
    scvtf  s10, x1
    str    s10, [x14], #4
    str    s6, [x14], #4
    str    s5, [x14], #4
    str    s0, [x14], #4
    str    s1, [x14], #4
    str    s2, [x14], #4
    str    s3, [x14], #4
    /* --- corner 2 (x1, y1) uv (u1, v1) --- */
    scvtf  s10, x2
    str    s10, [x14], #4
    scvtf  s10, x3
    str    s10, [x14], #4
    str    s6, [x14], #4
    str    s7, [x14], #4
    str    s0, [x14], #4
    str    s1, [x14], #4
    str    s2, [x14], #4
    str    s3, [x14], #4
    /* --- corner 3 (x0, y0) uv (u0, v0) --- */
    scvtf  s10, x0
    str    s10, [x14], #4
    scvtf  s10, x1
    str    s10, [x14], #4
    str    s4, [x14], #4
    str    s5, [x14], #4
    str    s0, [x14], #4
    str    s1, [x14], #4
    str    s2, [x14], #4
    str    s3, [x14], #4
    /* --- corner 4 (x1, y1) uv (u1, v1) --- */
    scvtf  s10, x2
    str    s10, [x14], #4
    scvtf  s10, x3
    str    s10, [x14], #4
    str    s6, [x14], #4
    str    s7, [x14], #4
    str    s0, [x14], #4
    str    s1, [x14], #4
    str    s2, [x14], #4
    str    s3, [x14], #4
    /* --- corner 5 (x0, y1) uv (u0, v1) --- */
    scvtf  s10, x0
    str    s10, [x14], #4
    scvtf  s10, x3
    str    s10, [x14], #4
    str    s4, [x14], #4
    str    s7, [x14], #4
    str    s0, [x14], #4
    str    s1, [x14], #4
    str    s2, [x14], #4
    str    s3, [x14], #4
.Lgq_full:
    ret
    .size nota_gl_quad, . - nota_gl_quad

    .section .rodata
    .p2align 3
.Linv:  .float 0.00480769, 0.00312500   /* 1/208, 1/320 */

    .text

/* ========================================================================= *
 * void nota_gl_frame(long w, long h)
 *
 * Begin one frame: bind the pipeline, clear, and reset the vertex count. The
 * reset is the important part - without it the batcher appends to the previous
 * frame's geometry, so every frame redraws everything ever emitted until the
 * queue fills and stays full.
 * ========================================================================= */
    .globl nota_gl_frame
    .type  nota_gl_frame, %function
nota_gl_frame:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    adrp   x19, nota_gl
    add    x19, x19, :lo12:nota_gl
    mov    x21, x0              /* w */
    mov    x22, x1              /* h */

    /* Reset the batcher. Without this the frame appends to everything emitted
       since launch, so the screen gradually turns into a smear of old geometry
       and then stops changing once the queue is full. */
    str    xzr, [x19, #GL_NVERTS]

    mov    w0, #0
    mov    w1, #0
    mov    w2, #0
    mov    w3, #1
    bl     glClearColor
    mov    w0, #GL_COLOR_BUFFER_BIT
    bl     glClear

    /* The viewport must match the window, or the projection in the vertex
       shader maps a different box than the one being cleared. */
    mov    w0, #0
    mov    w1, #0
    mov    w2, w21
    mov    w3, w22
    bl     glViewport

    ldr    w0, [x19, #GL_PROG]
    bl     glUseProgram
    mov    w0, #GL_TEXTURE0
    bl     glActiveTexture
    ldr    w0, [x19, #GL_TEX]
    bl     glBindTexture
    ldr    w0, [x19, #GL_U_TEX]
    mov    w1, #0
    bl     glUniform1i

    /* u_proj = (w, h), as two floats in the state block so the pointer stays
       valid for the duration of the call. */
    scvtf  s0, x21
    scvtf  s1, x22
    str    s0, [x19, #GL_PROJ_W]
    str    s1, [x19, #GL_PROJ_H]
    ldr    w0, [x19, #GL_U_PROJ]
    add    x1, x19, #GL_PROJ_W
    bl     glUniform2fv

    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_gl_frame, . - nota_gl_frame

/* ========================================================================= *
 * void nota_gl_flush(void)
 *
 * Upload the frame's quads in one buffer write and draw them in one call.
 * The whole point of batching is here: one glBufferSubData and one
 * glDrawArrays per frame, regardless of how much was queued.
 * ========================================================================= */
    .globl nota_gl_flush
    .type  nota_gl_flush, %function
nota_gl_flush:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    str    x19, [sp, #16]
    adrp   x19, nota_gl
    add    x19, x19, :lo12:nota_gl
    ldr    w9, [x19, #GL_NVERTS]
    cbz    w9, .Lgf_out          /* nothing queued: no work, no error */

    mov    w0, #GL_ARRAY_BUFFER
    ldr    w1, [x19, #GL_VBO]
    bl     glBindBuffer

    /* bytes = nverts * stride * 4 */
    mov    w10, #(GL_VSTRIDE * 4)
    mul    w10, w9, w10
    mov    w0, #GL_ARRAY_BUFFER
    mov    w1, #0
    mov    w2, w10
    adrp   x3, nota_gl_vbuf
    add    x3, x3, :lo12:nota_gl_vbuf
    mov    w4, #GL_STREAM_DRAW
    bl     glBufferSubData

    mov    w0, #GL_TRIANGLES
    mov    w1, #0
    mov    w2, w9
    bl     glDrawArrays
.Lgf_out:
    ldr    x19, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_gl_flush, . - nota_gl_flush

    .section .note.GNU-stack,"",%progbits
