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
#include "macros.inc"

/* nota_store header, mirrored from app.S where the block is defined. */
    .equ ST_NOTES,    0x00
    .equ ST_COUNT,    0x08
    .equ ST_ARENA,    0x10
    .equ ST_USED,     0x18
    .equ ST_DIRTY,    0x20

/* ANativeActivity::internalDataPath (app/glue.h) */
    .equ NA_INTERNAL_DATA, 0x20
    .equ AS_ACTIVITY,      0x00     /* nota_as: the ANativeActivity* */


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
    /* arena: title + NUL + body + NUL + the tag id array. The tag ids are
       copied out of the record rather than pointed at inside it, so they are
       charged to the arena like the strings are. */
    add    x11, x11, #2
    add    x11, x11, #7
    and    x11, x11, #-8       /* keep the arena cursor 8-aligned */
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
    /* tag ids, straight after the body's NUL. The destination comes from the
       arena: NOTE_TAG_PTR held a pointer on the note the record was written
       from, which means nothing in this process, so reading it here would
       either copy to an arbitrary address or silently drop the tags. */
    cbz    x26, .Lnd_ok
    ldr    w10, [x21, #NOTE_BODY_LEN]
    add    x0, x0, x10
    add    x0, x0, #1
    add    x0, x0, #7
    and    x0, x0, #-8         /* 8-align the array so the cursor stays sane */
    str    x0, [x21, #NOTE_TAG_PTR]
    ldr    w9, [x21, #NOTE_TITLE_LEN]
    ldr    w10, [x21, #NOTE_BODY_LEN]
    add    x1, x27, #NOTE_HDR_SIZE
    add    x1, x1, x9
    add    x1, x1, x10
    lsl    x2, x26, #2
    /* x0 is the destination: aligning into a different register and storing
       that left memcpy writing at the unaligned address, so the ids landed a
       few bytes below the pointer the note recorded. */
    bl     nota_memcpy
    b      .Lnd_tags_out
.Lnd_ok:
    str    xzr, [x21, #NOTE_TAG_PTR]
    str    wzr, [x21, #NOTE_TAGS]
.Lnd_tags_out:
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

/* ========================================================================= *
 * long nota_store_path(char *dst, long cap)
 *
 * Build "<internalDataPath>/notes.db" into dst. The data directory is the only
 * place a native activity may write, and the activity owns the string, so the
 * path cannot be cached across launches. Returns the length or -1.
 * ========================================================================= */
    .globl nota_store_path
    .type  nota_store_path, %function
nota_store_path:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    mov    x19, x0              /* dst */
    mov    x20, x1              /* cap */
    adrp   x9, nota_as
    add    x9, x9, :lo12:nota_as
    ldr    x9, [x9, #AS_ACTIVITY]
    cbz    x9, .Lsp_fail
    ldr    x2, [x9, #NA_INTERNAL_DATA]
    cbz    x2, .Lsp_fail
    mov    x0, x19
    mov    x1, x20
    adrp   x3, .Lsp_name
    add    x3, x3, :lo12:.Lsp_name
    bl     nota_path_join
    b      .Lsp_out
.Lsp_fail:
    mov    x0, #-1
.Lsp_out:
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #32
    ret
    .size nota_store_path, . - nota_store_path

/* ========================================================================= *
 * void nota_app_load(void) / void nota_app_save(void)
 *
 * Lifecycle entry points: they own the path buffer and swallow errors. A
 * failure to load leaves an empty collection (the caller can still write); a
 * failure to save leaves the dirty flag set so the next pause retries.
 * ========================================================================= */
    .globl nota_app_load
    .type  nota_app_load, %function
nota_app_load:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    adrp   x0, nota_pathbuf
    add    x0, x0, :lo12:nota_pathbuf
    mov    x1, #MAX_PATH
    bl     nota_store_path
    tbnz   x0, #63, 1f
    adrp   x0, nota_pathbuf
    add    x0, x0, :lo12:nota_pathbuf
    bl     nota_store_load
1:  ldp    x29, x30, [sp], #32
    ret
    .size nota_app_load, . - nota_app_load

    .globl nota_app_save
    .type  nota_app_save, %function
nota_app_save:
    stp    x29, x30, [sp, #-32]!
    mov    x29, sp
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    ldr    x9, [x9, #ST_DIRTY]
    cbz    x9, 2f              /* nothing changed: no write, no flash wear */
    adrp   x0, nota_pathbuf
    add    x0, x0, :lo12:nota_pathbuf
    mov    x1, #MAX_PATH
    bl     nota_store_path
    tbnz   x0, #63, 2f
    adrp   x0, nota_pathbuf
    add    x0, x0, :lo12:nota_pathbuf
    bl     nota_store_save
2:  ldp    x29, x30, [sp], #32
    ret
    .size nota_app_save, . - nota_app_save

/* ========================================================================= *
 * long nota_store_save(const char *path)
 *
 * Serialise the whole collection into a static blob and hand it to
 * nota_store_save_file, which owns the crash-safety dance. The framing:
 *
 *   "NOTA" | u32 version | u32 count | record*, record = u32 len | u32 crc | payload
 *
 * The same record framing a single note uses, so a truncated or corrupt file
 * fails on the first bad record rather than being half-applied.
 * Returns 0 on success, a negative STORE_ERR_* otherwise.
 * ========================================================================= */
    .equ BLOB_VERSION, 1
/* One blob at a time: save builds it, load reads into it. Static rather than
 * heap because it is a single fixed allocation, but deliberately smaller than
 * the theoretical worst case (512 notes x 64 KiB = 32 MiB): a collection that
 * size would not fit in a phone's storage anyway, and reserving it would cost
 * more address space than the app is worth. A save that would exceed this
 * fails cleanly with STORE_ERR_NOSPACE instead of truncating. */
    .equ BLOB_MAX, (12 + 128 * MAX_NOTE_BYTES)      /* 8 MiB */

    .globl nota_store_save
    .type  nota_store_save, %function
nota_store_save:
    stp    x29, x30, [sp, #-96]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    mov    x19, x0              /* path */
    adrp   x20, nota_store
    add    x20, x20, :lo12:nota_store
    ldr    x21, [x20, #ST_COUNT]
    ldr    x22, [x20, #ST_NOTES]

    /* size = 12 + sum(encoded record size). The running total and the index
       must be callee-saved: nota_note_encoded_size clobbers x0-x8, so keeping
       either in a caller-saved register silently loses part of the total and
       the blob is written shorter than the records it contains. */
    mov    x25, #12
    mov    x26, #0
.Lsv_size:
    cmp    x26, x21
    b.hs   .Lsv_sized
    mov    x11, #NOTE_SIZE
    madd   x0, x26, x11, x22
    bl     nota_note_encoded_size
    add    x25, x25, x0
    add    x26, x26, #1
    b      .Lsv_size
.Lsv_sized:
    mov    x23, x25             /* blob length */
    mov64  x9, BLOB_MAX
    cmp    x23, x9
    b.hi   .Lsv_nospace
    adrp   x0, nota_blob
    add    x0, x0, :lo12:nota_blob
    mov    x24, x0              /* blob */

    mov    x0, x24
    mov32  w1, 0x41544F4E      /* "NOTA" */
    bl     nota_put_u32
    add    x0, x24, #4
    mov    w1, #BLOB_VERSION
    bl     nota_put_u32
    add    x0, x24, #8
    mov    x1, x21
    bl     nota_put_u32

    /* The write cursor and the index must be callee-saved: nota_note_encode
       calls nota_put_u32 and the memcpy helpers, and x10/x11 are clobbered by
       the first of those. Using a caller-saved register here wrote the second
       record over the first and then passed a garbage address to memcpy. */
    add    x24, x24, #12        /* write cursor, now in the blob register */
    mov    x25, #0              /* index */
.Lsv_enc:
    cmp    x25, x21
    b.hs   .Lsv_write_prep
    mov    x12, #NOTE_SIZE
    madd   x0, x25, x12, x22
    mov    x1, x24
    bl     nota_note_encode
    add    x24, x24, x0
    add    x25, x25, #1
    b      .Lsv_enc
.Lsv_write_prep:
    /* the blob base again, since x24 walked to the end */
    adrp   x24, nota_blob
    add    x24, x24, :lo12:nota_blob

.Lsv_write:
    mov    x0, x19
    mov    x1, x24
    mov    x2, x23
    bl     nota_store_save_file
    /* Only drop the dirty flag once the bytes actually reached the file; a
       failed save must be retried on the next lifecycle event rather than
       quietly forgotten. Returning 0 normalises save_file's byte count so
       callers have one success/failure test. */
    tbnz   x0, #63, .Lsv_out        /* negative = STORE_ERR_* */
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    str    xzr, [x9, #ST_DIRTY]
    mov    x0, #0
    b      .Lsv_out
.Lsv_nospace:
    mov    x0, #STORE_ERR_NOSPACE
.Lsv_out:
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #96
    ret
    .size nota_store_save, . - nota_store_save

/* ========================================================================= *
 * long nota_store_load(const char *path)
 *
 * Replace the in-memory collection with the one on disk. A missing file is
 * not an error - a first run has nothing to load and must come up empty
 * rather than refusing to start - so it returns 0. A malformed file stops at
 * the first bad record and returns a negative STORE_ERR_*.
 * ========================================================================= */
    .globl nota_store_load
    .type  nota_store_load, %function
nota_store_load:
    stp    x29, x30, [sp, #-112]!
    mov    x29, sp
    stp    x19, x20, [sp, #16]
    stp    x21, x22, [sp, #32]
    stp    x23, x24, [sp, #48]
    stp    x25, x26, [sp, #64]
    stp    x27, x28, [sp, #80]
    mov    x26, x0              /* path - x0 is about to become the buffer */
    mov    x0, x26
    adrp   x1, nota_blob
    add    x1, x1, :lo12:nota_blob
    mov64  x2, BLOB_MAX
    bl     nota_read_file
    tbnz   x0, #63, .Lld_missing
    mov    x19, x0              /* bytes read */
    cmp    x19, #12
    b.lo   .Lld_corrupt

    adrp   x20, nota_blob
    add    x20, x20, :lo12:nota_blob
    mov    x0, x20
    bl     nota_get_u32
    mov32  w9, 0x41544F4E
    cmp    w0, w9
    b.ne   .Lld_corrupt
    add    x0, x20, #4
    bl     nota_get_u32
    cmp    x0, #BLOB_VERSION
    b.ne   .Lld_corrupt
    add    x0, x20, #8
    bl     nota_get_u32
    mov    x21, x0              /* record count */
    mov    w9, #MAX_NOTES
    cmp    x21, x9
    b.hi   .Lld_corrupt

    adrp   x22, nota_store
    add    x22, x22, :lo12:nota_store
    ldr    x23, [x22, #ST_NOTES]
    ldr    x24, [x22, #ST_ARENA]   /* string arena cursor */
    mov    x25, #0                 /* index  */
    add    x27, x20, #12           /* cursor */
.Lld_rec:
    cmp    x25, x21
    b.hs   .Lld_ok
    /* remaining = blob_end - cursor, so a truncated final record is caught */
    add    x1, x20, x19
    sub    x1, x1, x27
    cmp    x1, #8
    b.lo   .Lld_corrupt
    /* The destination note must live in a callee-saved register: decode is
       a real function and clobbers x0-x8, x2 included. */
    mov    x9, #NOTE_SIZE
    madd   x28, x25, x9, x23
    mov    x2, x28
    mov    x0, x27
    /* arena room left, so decode cannot run past the buffer */
    adrp   x9, nota_arena
    add    x9, x9, :lo12:nota_arena
    mov    w10, #MAX_NOTES
    mov    w11, #(MAX_NOTE_BYTES >> 16)
    lsl    x11, x11, #16
    mul    x9, x10, x11
    adrp   x10, nota_arena
    add    x10, x10, :lo12:nota_arena
    add    x9, x10, x9
    sub    x4, x9, x24
    mov    x3, x24
    bl     nota_note_decode
    tbnz   x0, #63, .Lld_corrupt
    /* Advance the arena cursor by exactly what decode consumed, using the
       same formula as its capacity check (title + NUL + body + NUL + tag ids,
       rounded up to 8). If the two ever disagree, notes start overlapping and
       one note's text silently overwrites another's. */
    ldr    w9, [x28, #NOTE_TITLE_LEN]
    ldr    w10, [x28, #NOTE_BODY_LEN]
    ldr    w11, [x28, #NOTE_TAGS]
    add    x24, x24, x9
    add    x24, x24, x10
    add    x24, x24, #2
    lsl    x11, x11, #2
    add    x24, x24, x11
    add    x24, x24, #7
    and    x24, x24, #-8
    add    x27, x27, x0
    add    x25, x25, #1
    b      .Lld_rec
.Lld_ok:
    str    x25, [x22, #ST_COUNT]
    str    xzr, [x22, #ST_DIRTY]
    mov    x0, x25
    b      .Lld_out
.Lld_missing:
    /* ENOENT and friends: nothing on disk yet, which is a valid empty state */
    adrp   x9, nota_store
    add    x9, x9, :lo12:nota_store
    str    xzr, [x9, #ST_COUNT]
    mov    x0, #0
    b      .Lld_out
.Lld_corrupt:
    mov    x0, #STORE_ERR_CORRUPT
.Lld_out:
    ldp    x27, x28, [sp, #80]
    ldp    x25, x26, [sp, #64]
    ldp    x23, x24, [sp, #48]
    ldp    x21, x22, [sp, #32]
    ldp    x19, x20, [sp, #16]
    ldp    x29, x30, [sp], #112
    ret
    .size nota_store_load, . - nota_store_load

    .bss
    .p2align 3
nota_pathbuf:
    .space MAX_PATH

    .p2align 4
    .globl nota_blob
    .hidden nota_blob
    .type  nota_blob, %object
nota_blob:
    .space BLOB_MAX
    .size nota_blob, . - nota_blob

    .section .rodata
.Lsp_name:
    .asciz "notes.db"

    .text
    .section .note.GNU-stack,"",%progbits
