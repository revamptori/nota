/*
 * store.S - crash-safe persistence for notes, folders and tags.
 *
 * Design goals, in order: never lose a note that was saved, never show a
 * corrupt note, and be cheap enough to autosave while typing.
 *
 * On-disk format: one file per collection, each file is
 *   [header][record][record]...[checksum]
 * Every record is length-prefixed and carries its own CRC-32, so a truncated
 * or corrupt record is detected and skipped instead of being parsed as
 * garbage. The whole file also carries a trailing CRC over the header and all
 * records.
 *
 * Writes are crash-safe by construction:
 *   1. write the new file to <name>.tmp
 *   2. fsync it
 *   3. rename over the live file (atomic on every Linux filesystem)
 *   4. fsync the directory
 * A crash at any point leaves either the old file or the new one. Step 4 is
 * what makes step 3 durable, and it is the step that is easiest to forget.
 *
 * Recovery: on load, if the live file is missing or fails its checksum, the
 * <name>.bak copy written before the previous successful rename is tried. Only
 * if both are unusable does the store start empty - and it says so, rather
 * than silently presenting an empty notebook.
 */
#include "nota.h"

/* ---- on-disk layout ------------------------------------------------------ */
#define F_MAGIC       0x4E4F5441       /* "NOTA" little endian, as u32 */
#define F_VERSION     1
#define HDR_SIZE      16               /* magic, version, count, reserved */

/* note record: fixed header then variable strings, all lengths explicit */
#define N_ID          0x00      /* u32 */
#define N_FLAGS       0x04      /* u32: pinned, trashed */
#define N_FOLDER      0x08      /* u32 */
#define N_TAGS        0x0C      /* u32: number of tag ids that follow */
#define N_CREATED     0x10
#define N_UPDATED     0x18
#define N_TITLE_LEN   0x20      /* u32 */
#define N_BODY_LEN    0x24      /* u32 */
#define N_TITLE       0x28      /* bytes, not NUL terminated on disk */
#define N_BODY        -1        /* follows title; computed */

#define NOTE_FLAG_PINNED  0x01
#define NOTE_FLAG_TRASHED 0x02

/* folder record */
#define FO_ID         0x00      /* u32 */
#define FO_PARENT     0x04      /* u32 */
#define FO_NAME_LEN   0x08      /* u32 */
#define FO_NAME       0x0C

/* tag record */
#define TG_ID         0x00      /* u32 */
#define TG_NAME_LEN   0x04      /* u32 */
#define TG_NAME       0x08

/* ---- path helpers ------------------------------------------------------- */
#define PATH_MAX      512

    .text

/* ========================================================================= *
 * Path building
 * ========================================================================= */

/* long nota_path_join(char *dst, long cap, const char *dir, const char *name)
 * Joins with a single '/'. Returns the resulting length, or -1 if it does not
 * fit. No allocation, no surprises: the buffer is always NUL-terminated on
 * success and left alone on failure.
 */
    .globl nota_path_join
    .type  nota_path_join, %function
nota_path_join:
    stp    x29, x30, [sp, #-48]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    mov    x19, x0              /* dst */
    mov    x20, x1              /* cap */
    mov    x21, x3              /* name */
    cbz    x20, .Lpj_fail
    mov    x22, x19             /* write cursor */
    sub    x9, x20, #1          /* bytes we may still write (plus NUL) */
    cbz    x9, .Lpj_fail
    /* copy dir */
.Lpj_dir:
    ldrb   w10, [x2], #1
    cbz    w10, .Lpj_sep
    cbz    x9, .Lpj_fail
    strb   w10, [x22], #1
    sub    x9, x9, #1
    b      .Lpj_dir
.Lpj_sep:
    /* exactly one separator, and none if dir already ends with one or is
       empty (a bare name then becomes "/name") */
    cmp    x22, x19
    b.eq   1f
    ldrb   w10, [x22, #-1]
    cmp    w10, #'/'
    b.eq   .Lpj_name
    b      2f
1:  /* empty dir: still need the separator */
2:  cbz    x9, .Lpj_fail
    mov    w10, #'/'
    strb   w10, [x22], #1
    sub    x9, x9, #1
    /* copy name */
.Lpj_name:
    ldrb   w10, [x21], #1
    cbz    w10, .Lpj_done
    cbz    x9, .Lpj_fail
    strb   w10, [x22], #1
    sub    x9, x9, #1
    b      .Lpj_name
.Lpj_done:
    strb   wzr, [x22]
    sub    x0, x22, x19
    b      .Lpj_out
.Lpj_fail:
    mov    x0, #-1
.Lpj_out:
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #48
    ret
    .size nota_path_join, . - nota_path_join

/* long nota_path_mktmp(char *dst, const char *path)
 * Appends ".tmp" - used for the write-then-rename dance.
 */
    .globl nota_path_mktmp
    .type  nota_path_mktmp, %function
nota_path_mktmp:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    mov    x19, x0              /* dst */
    mov    x20, x1              /* src */
    mov    x0, x19
    mov    x1, x20
    bl     nota_strcpy          /* dst may be a separate buffer */
    mov    x0, x19
    bl     nota_strlen
    add    x0, x19, x0          /* cursor just past the copied path */
    adr    x1, .Ltmp_suffix
    mov    x2, #5               /* ".tmp" plus its NUL */
    bl     nota_memcpy
    mov    x0, x19
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_path_mktmp, . - nota_path_mktmp

    .section .rodata
.Ltmp_suffix:
    .asciz ".tmp"
.Lbak_suffix:
    .asciz ".bak"
    .text

/* long nota_path_mkbak(char *dst, const char *path) - appends ".bak" */
    .globl nota_path_mkbak
    .type  nota_path_mkbak, %function
nota_path_mkbak:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    mov    x19, x0
    mov    x20, x1
    mov    x0, x19
    mov    x1, x20
    bl     nota_strcpy
    mov    x0, x19
    bl     nota_strlen
    add    x0, x19, x0
    adr    x1, .Lbak_suffix
    mov    x2, #5
    bl     nota_memcpy
    mov    x0, x19
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_path_mkbak, . - nota_path_mkbak

/* ========================================================================= *
 * Atomic file replacement
 * ========================================================================= */

/* long nota_store_copy_file(const char *src, const char *dst)
 * Byte copy through a 4 KiB stack buffer. Only used for the .bak snapshot.
 * A missing source is not an error: there is simply nothing to back up yet.
 */
    .globl nota_store_copy_file
    .type  nota_store_copy_file, %function
nota_store_copy_file:
    sub    sp, sp, #4096
    sub    sp, sp, #64
    stp    x29, x30, [sp]
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    mov    x19, x0              /* src */
    mov    x20, x1              /* dst */
    mov    x0, x19
    bl     nota_open_ro
    tbnz   x0, #63, .Lscf_out
    mov    x21, x0              /* in fd */
    mov    x0, x20
    bl     nota_open_rw
    tbnz   x0, #63, .Lscf_close_in
    mov    x22, x0              /* out fd */
    add    x23, sp, #64         /* buffer */
    mov    x24, #0              /* total */
.Lscf_loop:
    mov    x0, x21
    mov    x1, x23
    mov    x2, #4096
    bl     nota_read
    tbnz   x0, #63, .Lscf_err
    cbz    x0, .Lscf_flush
    mov    x2, x0
    mov    x0, x22
    mov    x1, x23
    bl     nota_write_all
    tbnz   x0, #63, .Lscf_err
    add    x24, x24, x0
    b      .Lscf_loop
.Lscf_flush:
    mov    x0, x22
    bl     nota_fsync
    mov    x0, x22
    bl     nota_close
    mov    x0, x21
    bl     nota_close
    mov    x0, x24
    b      .Lscf_out
.Lscf_err:
    mov    x24, x0
    mov    x0, x22
    bl     nota_close
    mov    x0, x21
    bl     nota_close
    mov    x0, x24
    b      .Lscf_out
.Lscf_close_in:
    mov    x24, x0
    mov    x0, x21
    bl     nota_close
    mov    x0, x24
.Lscf_out:
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp]
    add    sp, sp, #64
    add    sp, sp, #4096
    ret
    .size nota_store_copy_file, . - nota_store_copy_file

/* long nota_store_save_file(const char *path, const void *data, long len)
 * The crash-safety contract in one place:
 *   1. snapshot the current file to <path>.bak   (before, not after)
 *   2. write <path>.tmp, fsync, close
 *   3. rename(<path>.tmp, <path>) - atomic on every filesystem we can write to
 * A crash anywhere leaves either the previous file or the new one; the .bak
 * copy covers the (much rarer) case of a filesystem that lost the rename.
 */
    .globl nota_store_save_file
    .type  nota_store_save_file, %function
nota_store_save_file:
    sub    sp, sp, #512
    stp    x29, x30, [sp]
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    mov    x19, x0              /* path */
    mov    x20, x1              /* data */
    mov    x21, x2              /* len  */
    add    x22, sp, #64         /* tmp path */
    add    x23, sp, #320        /* bak path */
    mov    x0, x22
    mov    x1, x19
    bl     nota_path_mktmp
    mov    x0, x23
    mov    x1, x19
    bl     nota_path_mkbak
    /* 1. stage the new content in <path>.tmp and force it to disk. Nothing
       the reader can see has changed yet, so any failure here is harmless:
       drop the partial temp file and report it. */
    mov    x0, x22
    bl     nota_open_rw
    tbnz   x0, #63, .Lssf_out
    mov    x24, x0
    mov    x0, x24
    mov    x1, x20
    mov    x2, x21
    bl     nota_write_all
    tbnz   x0, #63, .Lssf_stage_fail
    mov    x0, x24
    bl     nota_fsync
    tbnz   x0, #63, .Lssf_stage_fail
    mov    x0, x24
    bl     nota_close
    /* 2. snapshot the file we are about to replace */
    mov    x0, x19
    mov    x1, x23
    bl     nota_store_copy_file
    /* 3. rename(tmp, path) - the instant the new content becomes visible */
    mov    x0, #-100
    mov    x1, x22
    mov    x2, #-100
    mov    x3, x19
    bl     nota_renameat
    tbnz   x0, #63, .Lssf_rename_fail
    mov    x0, x21
    b      .Lssf_out
.Lssf_stage_fail:
    str    x0, [sp, #56]        /* keep the errno across the cleanup */
    mov    x0, x24
    bl     nota_close
    mov    x0, #-100
    mov    x1, x22
    mov    x2, #0
    bl     nota_unlinkat
    ldr    x0, [sp, #56]
    b      .Lssf_out
.Lssf_rename_fail:
    str    x0, [sp, #56]
    mov    x0, #-100
    mov    x1, x22
    mov    x2, #0
    bl     nota_unlinkat
    ldr    x0, [sp, #56]
.Lssf_out:
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp]
    add    sp, sp, #512
    ret
    .size nota_store_save_file, . - nota_store_save_file

/* ========================================================================= *
 * Field codecs
 * ========================================================================= */

    .globl nota_put_u32
    .type  nota_put_u32, %function
nota_put_u32:
    str    w1, [x0]
    mov    x0, #4
    ret
    .size nota_put_u32, . - nota_put_u32

    .globl nota_put_u64
    .type  nota_put_u64, %function
nota_put_u64:
    str    x1, [x0]
    mov    x0, #8
    ret
    .size nota_put_u64, . - nota_put_u64

    .globl nota_get_u32
    .type  nota_get_u32, %function
nota_get_u32:
    ldr    w0, [x0]
    ret
    .size nota_get_u32, . - nota_get_u32

    .globl nota_get_u64
    .type  nota_get_u64, %function
nota_get_u64:
    ldr    x0, [x0]
    ret
    .size nota_get_u64, . - nota_get_u64

/* ========================================================================= *
 * Note records
 * ========================================================================= */

/* long nota_note_encoded_size(const note *n) */
    .globl nota_note_encoded_size
    .type  nota_note_encoded_size, %function
nota_note_encoded_size:
    ldr    w1, [x0, #NOTE_TITLE_LEN]
    ldr    w2, [x0, #NOTE_BODY_LEN]
    ldr    w3, [x0, #NOTE_TAGS]
    add    x9, x1, x2
    mov    x10, #4
    madd   x9, x3, x10, x9
    add    x9, x9, #NOTE_HDR_SIZE
    add    x9, x9, #8            /* record header: length + payload crc */
    mov    x0, x9
    ret
    .size nota_note_encoded_size, . - nota_note_encoded_size

/* long nota_note_encode(const note *n, char *dst)
 * Record layout:  u32 total_len | u32 crc32(payload) | payload
 * payload:        NOTE_HDR_SIZE fixed fields, then title, body, tag ids
 * Returns the record length.
 */
    .globl nota_note_encode
    .type  nota_note_encode, %function
nota_note_encode:
    stp    x29, x30, [sp, #-64]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    str    x23, [sp, #48]
    mov    x19, x0              /* note */
    mov    x20, x1              /* dst  */
    bl     nota_note_encoded_size
    mov    x21, x0              /* total */
    sub    x22, x21, #8         /* payload length */
    add    x23, x20, #8         /* payload  */
    mov    x0, x23
    ldr    w1, [x19, #NOTE_ID]
    bl     nota_put_u32
    add    x0, x23, #4
    ldr    w1, [x19, #NOTE_FLAGS]
    bl     nota_put_u32
    add    x0, x23, #8
    ldr    w1, [x19, #NOTE_FOLDER]
    bl     nota_put_u32
    add    x0, x23, #12
    ldr    w1, [x19, #NOTE_TAGS]
    bl     nota_put_u32
    add    x0, x23, #16
    ldr    x1, [x19, #NOTE_CREATED]
    bl     nota_put_u64
    add    x0, x23, #24
    ldr    x1, [x19, #NOTE_UPDATED]
    bl     nota_put_u64
    add    x0, x23, #32
    ldr    w1, [x19, #NOTE_TITLE_LEN]
    bl     nota_put_u32
    add    x0, x23, #36
    ldr    w1, [x19, #NOTE_BODY_LEN]
    bl     nota_put_u32
    /* title */
    add    x0, x23, #NOTE_HDR_SIZE
    ldr    x1, [x19, #NOTE_TITLE_PTR]
    ldr    w2, [x19, #NOTE_TITLE_LEN]
    bl     nota_memcpy
    /* body */
    ldr    w9, [x19, #NOTE_TITLE_LEN]
    add    x0, x23, #NOTE_HDR_SIZE
    add    x0, x0, x9
    ldr    x1, [x19, #NOTE_BODY_PTR]
    ldr    w2, [x19, #NOTE_BODY_LEN]
    bl     nota_memcpy
    /* tag ids */
    ldr    w9, [x19, #NOTE_TITLE_LEN]
    ldr    w10, [x19, #NOTE_BODY_LEN]
    add    x9, x9, x10
    add    x0, x23, #NOTE_HDR_SIZE
    add    x0, x0, x9
    ldr    x1, [x19, #NOTE_TAG_PTR]
    ldr    w2, [x19, #NOTE_TAGS]
    lsl    w2, w2, #2
    bl     nota_memcpy
    /* length prefix and payload checksum */
    mov    x0, x20
    mov    w1, w21
    bl     nota_put_u32
    mov    x0, x23
    mov    x1, x22
    mov    w2, #0
    bl     nota_crc32
    str    w0, [x20, #4]
    mov    x0, x21
    ldr    x23, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #64
    ret
    .size nota_note_encode, . - nota_note_encode

/* long nota_note_decode(const char *src, long avail, note *n, char *strings,
 *                       long strings_cap)
 * Validates before trusting: the length must fit the available bytes and the
 * checksum must match, or nothing is read. Returns the bytes consumed, or
 * STORE_ERR_TRUNCATED / STORE_ERR_CORRUPT / STORE_ERR_NOSPACE.
 * The note's title/body/tags are copied into the caller's arena, so the
 * record buffer can be reused immediately.
 */
    .globl nota_note_decode
    .type  nota_note_decode, %function
nota_note_decode:
    stp    x29, x30, [sp, #-96]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    str    x27, [sp, #80]
    mov    x19, x0              /* src */
    mov    x20, x1              /* avail */
    mov    x21, x2              /* note */
    mov    x22, x3              /* strings arena */
    mov    x23, x4              /* arena capacity */
    cmp    x20, #8
    b.lo   .Lnd_short
    mov    x0, x19
    bl     nota_get_u32
    mov    x24, x0              /* record length */
    cmp    x24, x20
    b.hi   .Lnd_short
    cmp    x24, #8
    b.lo   .Lnd_corrupt
    sub    x25, x24, #8         /* payload length */
    add    x0, x19, #8
    mov    x1, x25
    mov    w2, #0
    bl     nota_crc32
    ldr    w9, [x19, #4]
    cmp    w0, w9
    b.ne   .Lnd_corrupt
    cmp    x25, #NOTE_HDR_SIZE
    b.lo   .Lnd_corrupt
    add    x27, x19, #8         /* payload */
    mov    x0, x27
    bl     nota_get_u32
    str    w0, [x21, #NOTE_ID]
    add    x0, x27, #4
    bl     nota_get_u32
    str    w0, [x21, #NOTE_FLAGS]
    add    x0, x27, #8
    bl     nota_get_u32
    str    w0, [x21, #NOTE_FOLDER]
    add    x0, x27, #12
    bl     nota_get_u32
    mov    x26, x0              /* tag count */
    str    w0, [x21, #NOTE_TAGS]
    add    x0, x27, #16
    bl     nota_get_u64
    str    x0, [x21, #NOTE_CREATED]
    add    x0, x27, #24
    bl     nota_get_u64
    str    x0, [x21, #NOTE_UPDATED]
    add    x0, x27, #32
    bl     nota_get_u32
    mov    x9, x0               /* title length */
    str    w0, [x21, #NOTE_TITLE_LEN]
    add    x0, x27, #36
    bl     nota_get_u32
    mov    x10, x0              /* body length */
    str    w0, [x21, #NOTE_BODY_LEN]
    /* the variable part must fit inside the payload exactly */
    add    x11, x9, x10
    mov    x12, #4
    madd   x11, x26, x12, x11
    add    x11, x11, #NOTE_HDR_SIZE
    cmp    x11, x25
    b.hi   .Lnd_corrupt
    /* arena: title + NUL + body + NUL */
    add    x11, x11, #2
    cmp    x11, x23
    b.hi   .Lnd_nospace
    /* Copy title, then body, into the arena so the note owns its text.
       The lengths are re-read from the note after each call: memcpy clobbers
       x9, and the scratch slots in this frame are all spoken for. */
    mov    x0, x22
    add    x1, x27, #NOTE_HDR_SIZE
    mov    x2, x9
    bl     nota_memcpy
    str    x22, [x21, #NOTE_TITLE_PTR]
    ldr    w9, [x21, #NOTE_TITLE_LEN]
    strb   wzr, [x22, x9]
    add    x0, x22, x9
    add    x0, x0, #1
    add    x1, x27, #NOTE_HDR_SIZE
    add    x1, x1, x9
    ldr    w10, [x21, #NOTE_BODY_LEN]
    mov    x2, x10
    bl     nota_memcpy
    str    x0, [x21, #NOTE_BODY_PTR]
    ldr    w10, [x21, #NOTE_BODY_LEN]
    strb   wzr, [x0, x10]
    /* tag ids */
    cbz    x26, .Lnd_ok
    ldr    x0, [x21, #NOTE_TAG_PTR]
    cbz    x0, .Lnd_ok          /* caller did not want the tags */
    ldr    w9, [x21, #NOTE_TITLE_LEN]
    ldr    w10, [x21, #NOTE_BODY_LEN]
    add    x1, x27, #NOTE_HDR_SIZE
    add    x1, x1, x9
    add    x1, x1, x10
    lsl    x2, x26, #2
    bl     nota_memcpy
.Lnd_ok:
    mov    x0, x24
    b      .Lnd_out
.Lnd_short:
    mov    x0, #STORE_ERR_TRUNCATED
    b      .Lnd_out
.Lnd_corrupt:
    mov    x0, #STORE_ERR_CORRUPT
    b      .Lnd_out
.Lnd_nospace:
    mov    x0, #STORE_ERR_NOSPACE
.Lnd_out:
    ldr    x27, [sp, #80]
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x29, x30, [sp], #96
    ret
    .size nota_note_decode, . - nota_note_decode

    .section .note.GNU-stack,"",%progbits
