/*
 * sys.S - raw Linux syscalls and small utilities, no libc.
 *
 * NOTA calls the kernel directly. bionic's own syscall stubs are a thin
 * `svc #0` wrapper with the number in x8, so nothing is gained by going
 * through libc - and avoiding it keeps the shared object small and free of
 * libc constructor dependencies.
 *
 * AArch64 Linux (asm-generic) syscall numbers are used, which is what
 * Android's arm64 kernel exposes. Args go in x0..x5, the number in x8, and
 * the result comes back in x0. On error x0 holds a small negative value
 * (-errno): every caller here tests `x0 < 0`, so no separate errno is needed.
 */
#include "nota.h"

    .text

/* ------------------------------------------------------------------------- *
 * long nota_sys(long nr, long a0, long a1, long a2, long a3, long a4, long a5)
 * Generic trampoline, useful from tests and for syscalls with no wrapper.
 * ------------------------------------------------------------------------- */
    .globl nota_sys
    .type  nota_sys, %function
nota_sys:
    mov    x8, x0
    mov    x0, x1
    mov    x1, x2
    mov    x2, x3
    mov    x3, x4
    mov    x4, x5
    mov    x5, x6
    svc    #0
    ret
    .size nota_sys, . - nota_sys

/* ---- individual wrappers: keep the syscall table in one place -------------- */

.macro SYSCALL_WRAP name, nr
    .globl \name
    .type  \name, %function
\name:
    mov    x8, #\nr
    svc    #0
    ret
    .size \name, . - \name
.endm

/* long nota_openat(int dirfd, const char *path, int flags, int mode) */
    SYSCALL_WRAP nota_openat,   56
/* long nota_read(int fd, void *buf, size_t n) */
    SYSCALL_WRAP nota_read,     63
/* long nota_write(int fd, const void *buf, size_t n) */
    SYSCALL_WRAP nota_write,    64
/* long nota_close(int fd) */
    SYSCALL_WRAP nota_close,    57
/* long nota_lseek(int fd, long off, int whence) */
    SYSCALL_WRAP nota_lseek,    62
/* long nota_fsync(int fd) */
    SYSCALL_WRAP nota_fsync,    82
/* long nota_ftruncate(int fd, long len) */
    SYSCALL_WRAP nota_ftruncate, 46
/* long nota_fstatat(int dirfd, const char *path, void *st, int flags) */
    SYSCALL_WRAP nota_fstatat,  79
/* long nota_unlinkat(int dirfd, const char *path, int flags) */
    SYSCALL_WRAP nota_unlinkat, 35
/* long nota_mkdirat(int dirfd, const char *path, int mode) */
    SYSCALL_WRAP nota_mkdirat,  34
/* long nota_getdents64(int fd, void *buf, size_t n) */
    SYSCALL_WRAP nota_getdents64, 61
/* long nota_getpid(void) */
    SYSCALL_WRAP nota_getpid,   172

/* long nota_renameat(int olddirfd, const char *old, int newdirfd,
 *                    const char *new)
 * Atomic replace on every Linux filesystem NOTA can write to, which is the
 * primitive the crash-safe store is built on.
 */
    .globl nota_renameat
    .type  nota_renameat, %function
nota_renameat:
    mov    x8, #38
    svc    #0
    ret
    .size nota_renameat, . - nota_renameat

/* long nota_clock_gettime(int clk, struct timespec *ts)
 * clk 0 = CLOCK_REALTIME, 1 = CLOCK_MONOTONIC (what autosave timing uses).
 */
    .globl nota_clock_gettime
    .type  nota_clock_gettime, %function
nota_clock_gettime:
    mov    x8, #113
    svc    #0
    ret
    .size nota_clock_gettime, . - nota_clock_gettime

/* long nota_monotonic_ms(void) - milliseconds since boot. */
    .globl nota_monotonic_ms
    .type  nota_monotonic_ms, %function
nota_monotonic_ms:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    mov    x0, #1               /* CLOCK_MONOTONIC */
    add    x1, sp, #16          /* timespec, clear of the saved x29/x30 */
    bl     nota_clock_gettime
    ldr    x1, [sp, #16]        /* tv_sec */
    ldr    x2, [sp, #24]        /* tv_nsec */
    /* 1e6 is not a mov-encodable immediate: build it from two halves */
    movz   x3, #0x4240
    movk   x3, #0x000F, lsl #16
    udiv   x2, x2, x3           /* tv_nsec / 1000000 = whole ms */
    mov    x3, #1000
    mul    x1, x1, x3           /* tv_sec * 1000 */
    add    x0, x1, x2
    ldp    x29, x30, [sp], #32
    ret
    .size nota_monotonic_ms, . - nota_monotonic_ms

/* long nota_nanosleep(long ms) */
    .globl nota_sleep_ms
    .type  nota_sleep_ms, %function
nota_sleep_ms:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    /* 1e6 is not a mov-encodable immediate: build it from three 16-bit parts */
    movz   x2, #0x4240
    movk   x2, #0x000F, lsl #16
    udiv   x1, x0, x2           /* seconds */
    mul    x3, x1, x2
    sub    x0, x0, x3           /* remainder ms */
    mul    x0, x0, x2           /* nanoseconds */
    stp    x1, x0, [sp, #16]    /* struct timespec, clear of saved regs */
    add    x2, sp, #16          /* req */
    mov    x0, #1               /* CLOCK_MONOTONIC */
    mov    x1, #0               /* TIMER_ABSTIME clear: relative sleep */
    mov    x3, #0               /* no remainder pointer */
    mov    x8, #115             /* clock_nanosleep */
    svc    #0
    ldp    x29, x30, [sp], #32
    ret
    .size nota_sleep_ms, . - nota_sleep_ms

/* long nota_exit(int code) - never returns. */
    .globl nota_exit
    .type  nota_exit, %function
nota_exit:
    mov    x8, #94              /* exit_group */
    svc    #0
    ret
    .size nota_exit, . - nota_exit

/* ------------------------------------------------------------------------- *
 * File helpers built on the syscalls above.
 * ------------------------------------------------------------------------- */

/* long nota_open_rw(const char *path)  - open for read+write, create. */
    .globl nota_open_rw
    .type  nota_open_rw, %function
nota_open_rw:
    mov    x1, x0
    mov    x0, #-100            /* AT_FDCWD */
    mov    x2, #(2 | 64 | 512)  /* O_RDWR | O_CREAT | O_TRUNC */
    mov    x3, #0x1A4           /* 0644 */
    b      nota_openat
    .size nota_open_rw, . - nota_open_rw

/* long nota_open_ro(const char *path) */
    .globl nota_open_ro
    .type  nota_open_ro, %function
nota_open_ro:
    mov    x1, x0
    mov    x0, #-100
    mov    x2, #0               /* O_RDONLY */
    mov    x3, #0
    b      nota_openat
    .size nota_open_ro, . - nota_open_ro

/* long nota_write_all(int fd, const void *buf, long len)
 * Returns len on success, or the negative error. Short writes are retried;
 * this is what a crash-safe writer needs to trust.
 */
    .globl nota_write_all
    .type  nota_write_all, %function
nota_write_all:
    stp    x29, x30, [sp, #-48]!
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    mov    x29, sp
    mov    x19, x0              /* fd */
    mov    x20, x1              /* buf */
    mov    x21, x2              /* remaining */
    mov    x22, x2              /* original len */
.Lwa_loop:
    cbz    x21, .Lwa_ok
    mov    x0, x19
    mov    x1, x20
    mov    x2, x21
    bl     nota_write
    tbnz   x0, #63, .Lwa_err
    cbz    x0, .Lwa_err         /* zero progress: treat as EIO */
    add    x20, x20, x0
    sub    x21, x21, x0
    b      .Lwa_loop
.Lwa_err:
    b      .Lwa_out
.Lwa_ok:
    mov    x0, x22
.Lwa_out:
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_write_all, . - nota_write_all

/* long nota_read_full(int fd, void *buf, long len)
 * Reads until len bytes are read or EOF. Returns the byte count (>= 0).
 */
    .globl nota_read_full
    .type  nota_read_full, %function
nota_read_full:
    stp    x29, x30, [sp, #-48]!
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    mov    x29, sp
    mov    x19, x0
    mov    x20, x1
    mov    x21, x2
    mov    x22, #0              /* total */
.Lrf_loop:
    cmp    x22, x21
    b.hs   .Lrf_done
    mov    x0, x19
    mov    x1, x20
    mov    x2, x21
    sub    x2, x2, x22
    bl     nota_read
    tbnz   x0, #63, .Lrf_err
    cbz    x0, .Lrf_done        /* EOF */
    add    x20, x20, x0
    add    x22, x22, x0
    b      .Lrf_loop
.Lrf_err:
    b      .Lrf_out
.Lrf_done:
    mov    x0, x22
.Lrf_out:
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_read_full, . - nota_read_full

/* long nota_read_file(const char *path, void *buf, long max)
 * Reads a whole file. Returns byte count, or a negative errno.
 */
    .globl nota_read_file
    .type  nota_read_file, %function
nota_read_file:
    stp    x29, x30, [sp, #-48]!
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    mov    x29, sp
    mov    x19, x1              /* buf */
    mov    x20, x2              /* max */
    bl     nota_open_ro
    tbnz   x0, #63, .Lrf2_out
    mov    x21, x0              /* fd */
    mov    x0, x21
    mov    x1, x19
    mov    x2, x20
    bl     nota_read_full
    mov    x22, x0
    mov    x0, x21
    bl     nota_close
    mov    x0, x22
.Lrf2_out:
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_read_file, . - nota_read_file

/* long nota_write_file_sync(const char *path_tmp, const char *path_final,
 *                           const void *buf, long len)
 * The atomic-replace primitive used by every store write:
 *   create tmp -> write -> fsync -> close -> rename over the target.
 * A crash at any point leaves either the old file or the new one, never a
 * partial one. Returns len on success, or a negative errno.
 */
    .globl nota_write_file_sync
    .type  nota_write_file_sync, %function
nota_write_file_sync:
    stp    x29, x30, [sp, #-64]!
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    mov    x29, sp
    mov    x19, x0              /* tmp path */
    mov    x20, x1              /* final path */
    mov    x21, x2              /* buf */
    mov    x22, x3              /* len */
    bl     nota_open_rw
    tbnz   x0, #63, .Lwfs_out
    mov    x23, x0              /* fd */
    mov    x0, x23
    mov    x1, x21
    mov    x2, x22
    bl     nota_write_all
    tbnz   x0, #63, .Lwfs_close_err
    mov    x24, x0              /* bytes written */
    mov    x0, x23
    bl     nota_fsync
    tbnz   x0, #63, .Lwfs_close_err
    mov    x0, x23
    bl     nota_close
    /* rename(tmp, final) makes the new content visible atomically */
    mov    x0, #-100            /* AT_FDCWD */
    mov    x1, x19
    mov    x2, #-100
    mov    x3, x20
    bl     nota_renameat
    tbnz   x0, #63, .Lwfs_out
    mov    x0, x24
    b      .Lwfs_out
.Lwfs_close_err:
    mov    x24, x0
    mov    x0, x23
    bl     nota_close
    mov    x0, x24
.Lwfs_out:
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #64
    ret
    .size nota_write_file_sync, . - nota_write_file_sync

/* ------------------------------------------------------------------------- *
 * String utilities not covered by libc.S
 * ------------------------------------------------------------------------- */

/* long nota_strcpy(char *dst, const char *src) - returns dst. */
    .globl nota_strcpy
    .type  nota_strcpy, %function
nota_strcpy:
    mov    x9, x0
.Lsc_loop:
    ldrb   w2, [x1], #1
    strb   w2, [x0], #1
    cbnz   w2, .Lsc_loop
    mov    x0, x9
    ret
    .size nota_strcpy, . - nota_strcpy

/* long nota_strcat(char *dst, const char *src) - returns dst.
 * Bounded only by the caller's buffer; use nota_strlcat for untrusted input.
 */
    .globl nota_strcat
    .type  nota_strcat, %function
nota_strcat:
    mov    x9, x0
.Lca_find:
    ldrb   w2, [x0], #1
    cbnz   w2, .Lca_find
    sub    x0, x0, #1
.Lca_copy:
    ldrb   w2, [x1], #1
    strb   w2, [x0], #1
    cbnz   w2, .Lca_copy
    mov    x0, x9
    ret
    .size nota_strcat, . - nota_strcat

/* long nota_strlcat(char *dst, const char *src, long cap)
 * Appends with truncation; returns the resulting length (like strlcat).
 */
    .globl nota_strlcat
    .type  nota_strlcat, %function
nota_strlcat:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    str    x21, [sp, #32]
    mov    x19, x0              /* dst */
    mov    x20, x2              /* cap */
    mov    x21, x1              /* src: nota_strlen clobbers x1 */
    bl     nota_strlen
    mov    x1, x21
    mov    x3, x0              /* dlen */
    cmp    x3, x20
    b.hs   .Llc_trunc
    /* copy at most cap - dlen - 1 bytes, then NUL */
    sub    x4, x20, x3
    sub    x4, x4, #1
    add    x5, x19, x3
.Llc_copy:
    cbz    x4, .Llc_term
    ldrb   w6, [x1], #1
    cbz    w6, .Llc_term
    strb   w6, [x5], #1
    sub    x4, x4, #1
    add    x3, x3, #1
    b      .Llc_copy
.Llc_term:
    strb   wzr, [x5]
    mov    x0, x3
    b      .Llc_out
.Llc_trunc:
    mov    x0, x20             /* full buffer: report cap */
.Llc_out:
    ldr    x21, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_strlcat, . - nota_strlcat

/* long nota_strchr(const char *s, int c) - pointer to first c, or 0. */
    .globl nota_strchr
    .type  nota_strchr, %function
nota_strchr:
    and    w1, w1, #0xff
.Lsh_loop:
    ldrb   w2, [x0]
    cmp    w2, w1
    b.eq   .Lsh_out
    cbz    w2, .Lsh_none
    add    x0, x0, #1
    b      .Lsh_loop
.Lsh_none:
    mov    x0, #0
.Lsh_out:
    ret
    .size nota_strchr, . - nota_strchr

/* long nota_strrchr(const char *s, int c) */
    .globl nota_strrchr
    .type  nota_strrchr, %function
nota_strrchr:
    and    w1, w1, #0xff
    mov    x3, #0
.Lsr_loop:
    ldrb   w2, [x0]
    cmp    w2, w1
    b.ne   1f
    mov    x3, x0
1:  cbz    w2, .Lsr_out
    add    x0, x0, #1
    b      .Lsr_loop
.Lsr_out:
    mov    x0, x3
    ret
    .size nota_strrchr, . - nota_strrchr


/* long nota_now_ms(void) - wall clock in milliseconds.
 * Used for note timestamps and for double-tap detection. A clock that cannot
 * be read is not worth failing over: return 0 and let callers treat every
 * event as independent. */
    .globl nota_now_ms
    .type  nota_now_ms, %function
nota_now_ms:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    /* struct timespec on the stack: 16 bytes, tv_sec then tv_nsec */
    mov    w0, #1               /* CLOCK_MONOTONIC */
    add    x1, sp, #16
    bl     nota_clock_gettime
    tbnz   x0, #63, .Lnm_zero
    ldp    x9, x10, [sp, #16]   /* sec, nsec */
    mov    w11, #1000
    movz   x12, #(1000000 & 0xFFFF)
    movk   x12, #(1000000 >> 16), lsl #16
    sdiv   x10, x10, x12        /* nsec -> ms */
    madd   x0, x9, x11, x10
    b      .Lnm_out
.Lnm_zero:
    mov    x0, #0
.Lnm_out:
    ldp    x29, x30, [sp], #32
    ret
    .size nota_now_ms, . - nota_now_ms

/* long nota_utoa(char *dst, unsigned long v) - returns bytes written. */
    .globl nota_utoa
    .type  nota_utoa, %function
nota_utoa:
    /* x0 = dst, x1 = value. Digits are produced back-to-front into a small
       stack scratch buffer, then copied forwards. */
    sub    sp, sp, #32
    mov    x2, x1               /* working copy of the value */
    add    x4, sp, #24          /* one past the last digit slot */
    mov    x5, x4
    mov    x6, #10
.Lua_digits:
    udiv   x7, x2, x6
    msub   x8, x7, x6, x2       /* value % 10 */
    add    w8, w8, #'0'
    strb   w8, [x5, #-1]!
    mov    x2, x7
    cbnz   x2, .Lua_digits
    mov    x9, #0               /* bytes written */
.Lua_copy:
    cmp    x5, x4
    b.hs   .Lua_done
    ldrb   w8, [x5], #1
    strb   w8, [x0, x9]
    add    x9, x9, #1
    b      .Lua_copy
.Lua_done:
    strb   wzr, [x0, x9]
    mov    x0, x9
    add    sp, sp, #32
    ret
    .size nota_utoa, . - nota_utoa

/* long nota_itoa(char *dst, long v) - returns bytes written incl. the sign. */
    .globl nota_itoa
    .type  nota_itoa, %function
nota_itoa:
    /* x0 = dst, x1 = value. nota_utoa also takes (dst, value), so the sign is
       emitted into dst first and the digits appended after it. x19 holds the
       sign byte count because nota_utoa uses x9 as a scratch counter. */
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    str    x19, [sp, #16]
    mov    x19, #0              /* sign bytes written */
    tbz    x1, #63, .Lit_pos
    mov    w2, #'-'
    strb   w2, [x0], #1
    neg    x1, x1               /* INT64_MIN stays INT64_MIN; utoa is unsigned */
    mov    x19, #1
.Lit_pos:
    bl     nota_utoa
    add    x0, x0, x19
    ldr    x19, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_itoa, . - nota_itoa

/* unsigned nota_crc32(const void *data, long len, unsigned seed)
 * CRC-32 (IEEE 802.3, reflected, poly 0xEDB88320), bitwise so there is no
 * 1 KiB table in the binary. Used by the store to detect torn writes.
 */
    .globl nota_crc32
    .type  nota_crc32, %function
nota_crc32:
    /* x0 = data, x1 = len, w2 = seed. The seed register is also the working
       state, so it is complemented first and the result complemented back. */
    mvn    w2, w2
    mov    w2, w2              /* clear the upper half: this is a 32-bit state */
    cbz    x1, .Lcrc_done
.Lcrc_byte:
    ldrb   w3, [x0], #1
    eor    w2, w2, w3
    mov    w4, #8
.Lcrc_bit:
    tbz    w2, #0, 1f
    lsr    w2, w2, #1
    movz   w5, #0x8320          /* 0xEDB88320: low half then high half */
    movk   w5, #0xEDB8, lsl #16
    eor    w2, w2, w5
    b      2f
1:  lsr    w2, w2, #1
2:  subs   w4, w4, #1
    b.ne   .Lcrc_bit
    subs   x1, x1, #1
    b.ne   .Lcrc_byte
.Lcrc_done:
    mvn    w0, w2
    mov    w0, w0
    ret
    .size nota_crc32, . - nota_crc32

/* long nota_memcmp_n(const void *a, const void *b, long n) - memcmp alias used
 * by the store's index lookup (kept separate so callers read better).
 */
    .globl nota_memcmp_n
    .type  nota_memcmp_n, %function
nota_memcmp_n:
    b      nota_memcmp
    .size nota_memcmp_n, . - nota_memcmp_n

    .section .note.GNU-stack,"",%progbits
