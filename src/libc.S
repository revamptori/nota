/*
 * libc.S - string/memory primitives implemented by NOTA itself.
 *
 * NOTA links against bionic for allocation, files and I/O, but the hot
 * primitives below are ours: they are trivial in AArch64, avoid ABI-surprise
 * around bionic's fortify wrappers, and are unit-tested on the host.
 *
 * AArch64 AAPCS64: args x0-x7, return x0, x0-x18 caller-saved, x19-x28
 * callee-saved, SP 16-byte aligned, no red zone.
 */
#include "nota.h"

    .text

/* ------------------------------------------------------------------------- *
 * int nota_memcmp(const void *a, const void *b, size_t n)
 * ------------------------------------------------------------------------- */
    .globl nota_memcmp
    .type  nota_memcmp, %function
nota_memcmp:
    cbz    x2, .Lmc_eq
.Lmc_loop:
    ldrb   w3, [x0], #1
    ldrb   w4, [x1], #1
    cmp    w3, w4
    b.ne   .Lmc_ne
    subs   x2, x2, #1
    b.ne   .Lmc_loop
.Lmc_eq:
    mov    w0, #0
    ret
.Lmc_ne:
    sub    w0, w3, w4
    ret
    .size nota_memcmp, . - nota_memcmp

/* ------------------------------------------------------------------------- *
 * void *nota_memcpy(void *dst, const void *src, size_t n)
 * ------------------------------------------------------------------------- */
    .globl nota_memcpy
    .type  nota_memcpy, %function
nota_memcpy:
    mov    x9, x0
    cmp    x2, #16
    b.lo   .Lcp_tail
.Lcp_16:
    ldp    x3, x4, [x1], #16
    stp    x3, x4, [x0], #16
    sub    x2, x2, #16
    cmp    x2, #16
    b.hs   .Lcp_16
.Lcp_tail:
    cbz    x2, .Lcp_done
.Lcp_byte:
    ldrb   w3, [x1], #1
    strb   w3, [x0], #1
    subs   x2, x2, #1
    b.ne   .Lcp_byte
.Lcp_done:
    mov    x0, x9
    ret
    .size nota_memcpy, . - nota_memcpy

/* ------------------------------------------------------------------------- *
 * void *nota_memmove(void *dst, const void *src, size_t n)
 * ------------------------------------------------------------------------- */
    .globl nota_memmove
    .type  nota_memmove, %function
nota_memmove:
    cmp    x0, x1
    b.ls   nota_memcpy
    mov    x9, x0
    cbz    x2, .Lmm_done
    add    x0, x0, x2
    add    x1, x1, x2
.Lmm_back:
    ldrb   w3, [x1, #-1]!
    strb   w3, [x0, #-1]!
    subs   x2, x2, #1
    b.ne   .Lmm_back
    mov    x0, x9
.Lmm_done:
    ret
    .size nota_memmove, . - nota_memmove

/* ------------------------------------------------------------------------- *
 * void *nota_memset(void *dst, int c, size_t n)
 * ------------------------------------------------------------------------- */
    .globl nota_memset
    .type  nota_memset, %function
nota_memset:
    mov    x9, x0
    and    w1, w1, #0xff
    cbz    x2, .Lms_done
.Lms_loop:
    strb   w1, [x9], #1
    subs   x2, x2, #1
    b.ne   .Lms_loop
.Lms_done:
    mov    x0, x9
    ret
    .size nota_memset, . - nota_memset

/* ------------------------------------------------------------------------- *
 * size_t nota_strlen(const char *s)
 * ------------------------------------------------------------------------- */
    .globl nota_strlen
    .type  nota_strlen, %function
nota_strlen:
    mov    x1, x0
.Lsl_loop:
    ldrb   w2, [x1], #1
    cbnz   w2, .Lsl_loop
    sub    x0, x1, x0
    sub    x0, x0, #1
    ret
    .size nota_strlen, . - nota_strlen

/* ------------------------------------------------------------------------- *
 * size_t nota_strlcpy(char *dst, const char *src, size_t cap)
 * Returns bytes stored, excluding the NUL terminator.
 * ------------------------------------------------------------------------- */
    .globl nota_strlcpy
    .type  nota_strlcpy, %function
nota_strlcpy:
    cbz    x2, .Lsc_zero
    sub    x2, x2, #1
    mov    x3, #0
.Lsc_loop:
    cmp    x3, x2
    b.hs   .Lsc_term
    ldrb   w4, [x1, x3]
    cbz    w4, .Lsc_term
    strb   w4, [x0, x3]
    add    x3, x3, #1
    b      .Lsc_loop
.Lsc_term:
    strb   wzr, [x0, x3]
    mov    x0, x3
    ret
.Lsc_zero:
    mov    x0, #0
    ret
    .size nota_strlcpy, . - nota_strlcpy

/* ------------------------------------------------------------------------- *
 * int nota_strcmp(const char *a, const char *b)
 * ------------------------------------------------------------------------- */
    .globl nota_strcmp
    .type  nota_strcmp, %function
nota_strcmp:
.Lst_loop:
    ldrb   w2, [x0], #1
    ldrb   w3, [x1], #1
    cmp    w2, w3
    b.ne   .Lst_diff
    cbnz   w2, .Lst_loop
    mov    w0, #0
    ret
.Lst_diff:
    sub    w0, w2, w3
    ret
    .size nota_strcmp, . - nota_strcmp

/* ------------------------------------------------------------------------- *
 * int nota_strcasecmp(const char *a, const char *b)   (ASCII folding)
 * ------------------------------------------------------------------------- */
    .globl nota_strcasecmp
    .type  nota_strcasecmp, %function
nota_strcasecmp:
.Lscmp_loop:
    ldrb   w2, [x0], #1
    ldrb   w3, [x1], #1
    sub    w4, w2, #'A'
    cmp    w4, #25
    b.hi   1f
    add    w2, w2, #32
1:  sub    w4, w3, #'A'
    cmp    w4, #25
    b.hi   2f
    add    w3, w3, #32
2:  cmp    w2, w3
    b.ne   .Lsc2_diff
    cbnz   w2, .Lscmp_loop
    mov    w0, #0
    ret
.Lsc2_diff:
    sub    w0, w2, w3
    ret
    .size nota_strcasecmp, . - nota_strcasecmp

/* ------------------------------------------------------------------------- *
 * int nota_strstr_i(const char *hay, const char *needle)
 * ASCII case-insensitive substring search. Returns byte index or -1.
 * ------------------------------------------------------------------------- */
    .globl nota_strstr_i
    .type  nota_strstr_i, %function
nota_strstr_i:
    /* 80 bytes: x19-x26 + x29/x30, 16-byte aligned */
    stp    x19, x20, [sp, #-80]!
    stp    x21, x22, [sp, #16]
    stp    x23, x24, [sp, #32]
    stp    x25, x26, [sp, #48]
    stp    x29, x30, [sp, #64]
    mov    x29, sp
    mov    x19, x0
    mov    x20, x1
    bl     nota_strlen
    mov    x21, x0
    mov    x0, x20
    bl     nota_strlen
    mov    x22, x0
    mov    x23, #-1
    cbz    x22, .Lsi_zero
    cmp    x21, x22
    b.lo   .Lsi_out
    mov    x24, #0
    sub    x25, x21, x22
.Lsi_outer:
    cmp    x24, x25
    b.hi   .Lsi_out
    mov    x10, #0
.Lsi_inner:
    cmp    x10, x22
    b.hs   .Lsi_hit
    add    x11, x19, x24
    ldrb   w12, [x11, x10]
    ldrb   w13, [x20, x10]
    sub    w14, w12, #'A'
    cmp    w14, #25
    b.hi   3f
    add    w12, w12, #32
3:  sub    w14, w13, #'A'
    cmp    w14, #25
    b.hi   4f
    add    w13, w13, #32
4:  cmp    w12, w13
    b.ne   .Lsi_next
    add    x10, x10, #1
    b      .Lsi_inner
.Lsi_hit:
    mov    x23, x24
    b      .Lsi_out
.Lsi_next:
    add    x24, x24, #1
    b      .Lsi_outer
.Lsi_zero:
    mov    x23, #0
.Lsi_out:
    mov    x0, x23
    /* unwind in reverse order; the post-index load must come last */
    ldp    x29, x30, [sp, #64]
    ldp    x25, x26, [sp, #48]
    ldp    x23, x24, [sp, #32]
    ldp    x21, x22, [sp, #16]
    ldp    x19, x20, [sp], #80
    ret
    .size nota_strstr_i, . - nota_strstr_i

/* ------------------------------------------------------------------------- *
 * int nota_atoi(const char *s)
 * ------------------------------------------------------------------------- */
    .globl nota_atoi
    .type  nota_atoi, %function
nota_atoi:
    /* x0 = string; x1 = cursor; w2 = sign; w3 = accumulator */
    mov    x1, x0
    mov    w2, #1
    mov    w3, #0
.Lai_ws:
    ldrb   w4, [x1]
    cmp    w4, #' '
    b.ne   .Lai_sign
    add    x1, x1, #1
    b      .Lai_ws
.Lai_sign:
    cmp    w4, #'-'
    b.ne   .Lai_plus
    mov    w2, #-1
    add    x1, x1, #1
    b      .Lai_digits
.Lai_plus:
    cmp    w4, #'+'
    b.ne   .Lai_digits
    add    x1, x1, #1
.Lai_digits:
    ldrb   w4, [x1]
    sub    w5, w4, #'0'
    cmp    w5, #9
    b.hi   .Lai_done
    mov    w6, #10
    madd   w3, w3, w6, w5
    add    x1, x1, #1
    b      .Lai_digits
.Lai_done:
    mul    w0, w3, w2
    ret
    .size nota_atoi, . - nota_atoi

    .section .note.GNU-stack,"",%progbits
