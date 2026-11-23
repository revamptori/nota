/*
 * utf8.S - UTF-8 and Unicode handling for NOTA.
 *
 * The text model: note bodies are UTF-8 byte strings. Every editing operation
 * works on byte offsets *at code point boundaries*, so the cursor can never
 * land inside a multi-byte sequence. Helpers here are pure functions over
 * (pointer, length) so they are unit-testable without any Android dependency.
 *
 * Implemented:
 *   nota_utf8_len          code points in a byte range
 *   nota_utf8_next         advance one code point (returns new offset)
 *   nota_utf8_prev         retreat one code point
 *   nota_utf8_decode       decode one code point, returns offset after it
 *   nota_utf8_encode       encode a code point, returns bytes written
 *   nota_utf8_valid        validate a byte range
 *   nota_utf8_byte_to_cp   byte offset -> code point index
 *   nota_utf8_cp_to_byte   code point index -> byte offset
 *
 * All offsets are `long` (x-register) so 64-bit sizes are safe.
 */
#include "nota.h"

    .text

/* ------------------------------------------------------------------------- *
 * long nota_utf8_decode(const char *s, long *cp_out)
 * Decodes one code point at s. Returns the number of bytes consumed (1..4).
 * Invalid sequences decode as U+FFFD and consume 1 byte, which guarantees
 * forward progress and never splits a valid sequence incorrectly.
 * ------------------------------------------------------------------------- */
    .globl nota_utf8_decode
    .type  nota_utf8_decode, %function
nota_utf8_decode:
    ldrb   w2, [x0]
    cbz    w2, .Lud_nul
    cmp    w2, #0x80
    b.hs   .Lud_multi
    /* ASCII fast path */
    str    w2, [x1]
    mov    x0, #1
    ret
.Lud_nul:
    mov    w2, #0xFFFD          /* NUL is not a character; caller checks */
    str    w2, [x1]
    mov    x0, #0
    ret
.Lud_multi:
    /* 2-byte: 110xxxxx 10xxxxxx */
    and    w3, w2, #0xE0
    cmp    w3, #0xC0
    b.eq   .Lud_two
    and    w3, w2, #0xF0
    cmp    w3, #0xE0
    b.eq   .Lud_three
    and    w3, w2, #0xF8
    cmp    w3, #0xF0
    b.eq   .Lud_four
    b      .Lud_bad
.Lud_two:
    ldrb   w4, [x0, #1]
    and    w5, w4, #0xC0
    cmp    w5, #0x80
    b.ne   .Lud_bad
    and    w2, w2, #0x1F
    lsl    w2, w2, #6
    and    w4, w4, #0x3F
    orr    w2, w2, w4
    cmp    w2, #0x80            /* overlong encoding */
    b.lo   .Lud_bad
    str    w2, [x1]
    mov    x0, #2
    ret
.Lud_three:
    ldrb   w4, [x0, #1]
    ldrb   w6, [x0, #2]
    and    w5, w4, #0xC0
    cmp    w5, #0x80
    b.ne   .Lud_bad
    and    w5, w6, #0xC0
    cmp    w5, #0x80
    b.ne   .Lud_bad
    and    w2, w2, #0x0F
    lsl    w2, w2, #12
    and    w4, w4, #0x3F
    lsl    w4, w4, #6
    orr    w2, w2, w4
    and    w6, w6, #0x3F
    orr    w2, w2, w6
    cmp    w2, #0x800           /* overlong */
    b.lo   .Lud_bad
    /* UTF-16 surrogates (D800..DFFF) are not valid code points in UTF-8.
       cmp immediates are limited to 12 bits, so compare via a register. */
    and    w5, w2, #0xF800
    mov    w7, #0xD800
    cmp    w5, w7
    b.eq   .Lud_bad
    str    w2, [x1]
    mov    x0, #3
    ret
.Lud_four:
    ldrb   w4, [x0, #1]
    ldrb   w6, [x0, #2]
    ldrb   w7, [x0, #3]
    and    w5, w4, #0xC0
    cmp    w5, #0x80
    b.ne   .Lud_bad
    and    w5, w6, #0xC0
    cmp    w5, #0x80
    b.ne   .Lud_bad
    and    w5, w7, #0xC0
    cmp    w5, #0x80
    b.ne   .Lud_bad
    and    w2, w2, #0x07
    lsl    w2, w2, #18
    and    w4, w4, #0x3F
    lsl    w4, w4, #12
    orr    w2, w2, w4
    and    w6, w6, #0x3F
    lsl    w6, w6, #6
    orr    w2, w2, w6
    and    w7, w7, #0x3F
    orr    w2, w2, w7
    cmp    w2, #0x10000         /* overlong */
    b.lo   .Lud_bad
    mov    w5, #0x110000
    cmp    w2, w5               /* above Unicode range */
    b.hs   .Lud_bad
    str    w2, [x1]
    mov    x0, #4
    ret
.Lud_bad:
    mov    w2, #0xFFFD
    str    w2, [x1]
    mov    x0, #1               /* consume one byte, keep making progress */
    ret
    .size nota_utf8_decode, . - nota_utf8_decode

/* ------------------------------------------------------------------------- *
 * long nota_utf8_encode(char *dst, unsigned cp)
 * Writes the UTF-8 form of cp. Returns 1..4, or 0 if cp is not encodable.
 * ------------------------------------------------------------------------- */
    .globl nota_utf8_encode
    .type  nota_utf8_encode, %function
nota_utf8_encode:
    cmp    w1, #0x80
    b.hs   .Lue_two_or_more
    strb   w1, [x0]
    mov    x0, #1
    ret
.Lue_two_or_more:
    mov    w9, #0x800
    cmp    w1, w9
    b.hs   .Lue_three_or_more
    /* 110xxxxx 10xxxxxx */
    lsr    w2, w1, #6
    orr    w2, w2, #0xC0
    strb   w2, [x0]
    and    w3, w1, #0x3F
    orr    w3, w3, #0x80
    strb   w3, [x0, #1]
    mov    x0, #2
    ret
.Lue_three_or_more:
    mov    w9, #0x10000
    cmp    w1, w9
    b.hs   .Lue_four
    /* reject surrogates */
    mov    w9, #0xD800
    cmp    w1, w9
    b.lo   1f
    mov    w10, #0xE000
    cmp    w1, w10
    b.lo   .Lue_bad
1:  lsr    w2, w1, #12
    orr    w2, w2, #0xE0
    strb   w2, [x0]
    lsr    w3, w1, #6
    and    w3, w3, #0x3F
    orr    w3, w3, #0x80
    strb   w3, [x0, #1]
    and    w4, w1, #0x3F
    orr    w4, w4, #0x80
    strb   w4, [x0, #2]
    mov    x0, #3
    ret
.Lue_four:
    mov    w9, #0x110000
    cmp    w1, w9
    b.hs   .Lue_bad
    lsr    w2, w1, #18
    orr    w2, w2, #0xF0
    strb   w2, [x0]
    lsr    w3, w1, #12
    and    w3, w3, #0x3F
    orr    w3, w3, #0x80
    strb   w3, [x0, #1]
    lsr    w4, w1, #6
    and    w4, w4, #0x3F
    orr    w4, w4, #0x80
    strb   w4, [x0, #2]
    and    w5, w1, #0x3F
    orr    w5, w5, #0x80
    strb   w5, [x0, #3]
    mov    x0, #4
    ret
.Lue_bad:
    mov    x0, #0
    ret
    .size nota_utf8_encode, . - nota_utf8_encode

/* ------------------------------------------------------------------------- *
 * long nota_utf8_seqlen(unsigned char lead)
 * Number of bytes in the sequence introduced by `lead` (1..4), or 1 for a
 * byte that cannot start a sequence.
 * ------------------------------------------------------------------------- */
    .globl nota_utf8_seqlen
    .type  nota_utf8_seqlen, %function
nota_utf8_seqlen:
    /* Leaf helper: may only touch x0 and the scratch w9.
       Callers pass their state in x1..x4, so those must survive. */
    and    w9, w0, #0x80
    cbz    w9, .Lsl_one
    and    w9, w0, #0xE0
    cmp    w9, #0xC0
    b.eq   .Lsl_two
    and    w9, w0, #0xF0
    cmp    w9, #0xE0
    b.eq   .Lsl_three
    and    w9, w0, #0xF8
    cmp    w9, #0xF0
    b.eq   .Lsl_four
.Lsl_one:
    mov    x0, #1
    ret
.Lsl_two:
    mov    x0, #2
    ret
.Lsl_three:
    mov    x0, #3
    ret
.Lsl_four:
    mov    x0, #4
    ret
    .size nota_utf8_seqlen, . - nota_utf8_seqlen

/* ------------------------------------------------------------------------- *
 * long nota_utf8_next(const char *s, long len, long off)
 * Returns the offset of the next code point, clamped to len.
 * ------------------------------------------------------------------------- */
    .globl nota_utf8_next
    .type  nota_utf8_next, %function
nota_utf8_next:
    /* x0 = s, x1 = len, x2 = off. Calls a helper, so LR must be saved. */
    stp    x29, x30, [sp, #-16]!
    mov    x29, sp
    cmp    x2, x1
    b.hs   .Lun_at_end
    mov    x8, x0
    ldrb   w0, [x8, x2]
    bl     nota_utf8_seqlen
    add    x2, x2, x0
    cmp    x2, x1
    b.ls   .Lun_out
.Lun_at_end:
    mov    x2, x1
.Lun_out:
    mov    x0, x2
    ldp    x29, x30, [sp], #16
    ret
    .size nota_utf8_next, . - nota_utf8_next

/* ------------------------------------------------------------------------- *
 * long nota_utf8_prev(const char *s, long off)
 * Returns the offset of the previous code point start (>= 0).
 * ------------------------------------------------------------------------- */
    .globl nota_utf8_prev
    .type  nota_utf8_prev, %function
nota_utf8_prev:
    cbz    x1, .Lup_zero
    sub    x1, x1, #1
.Lup_back:
    cbz    x1, .Lup_out
    ldrb   w2, [x0, x1]
    and    w3, w2, #0xC0
    cmp    w3, #0x80            /* continuation byte: keep walking back */
    b.ne   .Lup_out
    sub    x1, x1, #1
    b      .Lup_back
.Lup_zero:
    mov    x1, #0
.Lup_out:
    mov    x0, x1
    ret
    .size nota_utf8_prev, . - nota_utf8_prev

/* ------------------------------------------------------------------------- *
 * long nota_utf8_len(const char *s, long len)
 * Number of code points (invalid bytes count as one each).
 * ------------------------------------------------------------------------- */
    .globl nota_utf8_len
    .type  nota_utf8_len, %function
nota_utf8_len:
    /* x0 = s, x1 = len, x3 = offset, x4 = count. The base pointer must live
       in a register that survives `bl`, because x0 is the return value. */
    stp    x29, x30, [sp, #-16]!
    mov    x29, sp
    mov    x8, x0
    mov    x3, #0
    mov    x4, #0
.Lul_loop:
    cmp    x3, x1
    b.hs   .Lul_done
    add    x4, x4, #1
    ldrb   w0, [x8, x3]
    bl     nota_utf8_seqlen
    add    x3, x3, x0
    b      .Lul_loop
.Lul_done:
    mov    x0, x4
    ldp    x29, x30, [sp], #16
    ret
    .size nota_utf8_len, . - nota_utf8_len

/* ------------------------------------------------------------------------- *
 * long nota_utf8_cp_to_byte(const char *s, long len, long cpidx)
 * Byte offset of code point `cpidx` (clamped to len).
 * ------------------------------------------------------------------------- */
    .globl nota_utf8_cp_to_byte
    .type  nota_utf8_cp_to_byte, %function
nota_utf8_cp_to_byte:
    /* x0 = s, x1 = len, x2 = code point index; x8 = base across `bl` */
    stp    x29, x30, [sp, #-16]!
    mov    x29, sp
    mov    x8, x0
    mov    x3, #0               /* byte offset */
    mov    x4, #0               /* cp index */
.Lcb_loop:
    cmp    x4, x2
    b.hs   .Lcb_done
    cmp    x3, x1
    b.hs   .Lcb_done
    ldrb   w0, [x8, x3]
    bl     nota_utf8_seqlen
    add    x3, x3, x0
    add    x4, x4, #1
    b      .Lcb_loop
.Lcb_done:
    cmp    x3, x1
    b.ls   1f
    mov    x3, x1
1:  mov    x0, x3
    ldp    x29, x30, [sp], #16
    ret
    .size nota_utf8_cp_to_byte, . - nota_utf8_cp_to_byte

/* ------------------------------------------------------------------------- *
 * long nota_utf8_byte_to_cp(const char *s, long byteoff)
 * Index of the code point containing byte `byteoff`: the number of complete
 * code points that end at or before it. For a byte offset sitting on a
 * boundary this is the index of the code point that starts there, so it is
 * also the natural cursor index; for an offset inside a sequence it is the
 * index of the sequence that contains it, never past it.
 * ------------------------------------------------------------------------- */
    .globl nota_utf8_byte_to_cp
    .type  nota_utf8_byte_to_cp, %function
nota_utf8_byte_to_cp:
    /* x0 = s, x1 = byte offset; x8 = base across `bl` */
    stp    x29, x30, [sp, #-16]!
    mov    x29, sp
    mov    x8, x0
    mov    x3, #0               /* byte cursor */
    mov    x4, #0               /* cp count */
.Lbc_loop:
    cmp    x3, x1
    b.hs   .Lbc_done
    ldrb   w0, [x8, x3]
    cbz    w0, .Lbc_done        /* NUL terminates: do not count past the end */
    bl     nota_utf8_seqlen
    add    x9, x3, x0           /* end offset of this code point */
    cmp    x9, x1
    b.hi   .Lbc_done            /* extends past byteoff: not a complete cp */
    mov    x3, x9
    add    x4, x4, #1
    b      .Lbc_loop
.Lbc_done:
    mov    x0, x4
    ldp    x29, x30, [sp], #16
    ret
    .size nota_utf8_byte_to_cp, . - nota_utf8_byte_to_cp

/* ------------------------------------------------------------------------- *
 * int nota_utf8_valid(const char *s, long len)
 * 1 if the range is well-formed UTF-8, else 0.
 * ------------------------------------------------------------------------- */
    .globl nota_utf8_valid
    .type  nota_utf8_valid, %function
nota_utf8_valid:
    stp    x29, x30, [sp, #-32]!
    stp    x19, x20, [sp, #16]
    mov    x29, sp
    mov    x19, x0
    mov    x20, x1
    mov    x9, #0                /* byte cursor (x2..x4 are clobbered) */
.Luv_loop:
    cmp    x9, x20
    b.hs   .Luv_ok
    add    x0, x19, x9
    sub    sp, sp, #16
    mov    x1, sp
    bl     nota_utf8_decode
    ldr    w3, [sp]
    add    sp, sp, #16
    cbz    x0, .Luv_ok            /* NUL: end of content */
    mov    w4, #0xFFFD
    cmp    w3, w4
    b.ne   .Luv_next
    /* U+FFFD is legitimate input, so only fail when the byte was not itself
       the start of a valid EF BF BD sequence. */
    ldrb   w10, [x19, x9]
    cmp    w10, #0xEF
    b.ne   .Luv_bad
.Luv_next:
    add    x9, x9, x0
    b      .Luv_loop
.Luv_ok:
    mov    w0, #1
    b      .Luv_out
.Luv_bad:
    mov    w0, #0
.Luv_out:
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_utf8_valid, . - nota_utf8_valid

    .section .note.GNU-stack,"",%progbits
