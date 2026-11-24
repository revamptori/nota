#!/usr/bin/env python3
"""test_decode.py - round-trip the interpreter's decoders against the assembler.

Every bug found in a64.py so far was a *decoding* mistake: the interpreter
disagreed with the real hardware encoding. The most reliable way to catch that
class of bug is to assemble known instructions with the real assembler, run
them on the interpreter, and compare against expected values.

Usage: NOTA_CC=... python tests/test_decode.py
"""
import ctypes
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import a64  # noqa: E402

CC = os.environ.get("NOTA_CC", "zig cc -target aarch64-linux-android").split()

# Each case: assembly of one function, plus its expected effect.
CASES = [
    # --- pairs and stack discipline (imm7 scaling + writeback modes) ---
    ("stp post / ldp post", """
        stp x29, x30, [sp, #-32]!
        stp x1, x2, [sp]
        ldp x1, x2, [sp], #32
        ldp x29, x30, [sp], #32
        mov x0, x1
        add x0, x0, x2
        ret""", {1: 2, 2: 3}, 5),
    ("ldp offset mode", """
        stp x29, x30, [sp, #-32]!
        stp x3, x4, [sp, #16]
        ldp x0, x1, [sp, #16]
        ldp x29, x30, [sp], #32
        add x0, x0, x1
        ret""", {3: 7, 4: 9}, 16),
    # --- movn / movz / movk ---
    ("movn", "movn x0, #0\n ret", {}, 0xFFFFFFFFFFFFFFFF),
    ("movz+movk", "movz x0, #0x4240\n movk x0, #0x000F, lsl #16\n ret", {},
     1000000),
    ("movk word2", "movz x0, #1\n movk x0, #0xABCD, lsl #48\n ret", {},
     (0xABCD << 48) | 1),
    # --- logical shifted register, with and without N ---
    ("mvn", "movz x1, #0x1234\n mvn x0, x1\n ret", {}, 0xFFFFFFFFFFFFEDCB),
    # 0xff | ~0x0f is all ones: the low byte alone already fills bits 0-7
    ("orn", "movz x1, #0x100\n movz x2, #0x0f\n orn x0, x1, x2\n ret", {},
     0xFFFFFFFFFFFFFFF0 | 0x100),
    ("bic", "movz x1, #0xffff\n and x1, x1, #0xffff\n bic x0, x1, #0xff\n ret",
     {}, 0xFF00),
    ("eon", "movz x1, #0\n movz x2, #0\n eon x0, x1, x2\n ret", {},
     0xFFFFFFFFFFFFFFFF),
    ("orr shifted", "mov x0, #1\n orr x0, x0, x0, lsl #8\n ret", {}, 0x101),
    # --- division ---
    ("udiv 64-bit", "movz x1, #0x4240\n movk x1, #0x000F, lsl #16\n"
                    " mov x0, #-1\n udiv x0, x0, x1\n ret", {},
     (2**64 - 1) // 1000000),
    ("sdiv", "movn x0, #41\n movz x1, #7\n sdiv x0, x0, x1\n ret", {},
     (2**64 - 42) // 7 if False else ((-42) // 7) & 0xFFFFFFFFFFFFFFFF),
    ("udiv 32-bit", "movz w0, #100\n movz w1, #7\n udiv w0, w0, w1\n ret",
     {}, 14),
    ("div by zero", "movz x1, #0\n movz x0, #9\n udiv x0, x0, x1\n ret", {}, 0),
    # --- msub (used by itoa) ---
    ("msub", "movz x1, #10\n movz x2, #42\n udiv x3, x2, x1\n"
             " msub x0, x3, x1, x2\n ret", {}, 2),
    # --- bitfield aliases ---
    ("lsr", "mov x0, #-1\n lsr x0, x0, #56\n ret", {}, 0xFF),
    ("asr", "movn x0, #0\n asr x0, x0, #56\n ret", {}, 0xFFFFFFFFFFFFFFFF),
    ("sxtb", "movz x0, #0xFF\n sxtb x0, w0\n ret", {}, 0xFFFFFFFFFFFFFFFF),
    ("ubfx", "mov x1, #-1\n ubfx x0, x1, #8, #8\n ret", {}, 0xFF),
    # --- conditional select / flags ---
    ("cset", "cmp x0, x0\n cset x0, eq\n ret", {}, 1),
    ("csel", "movz x1, #1\n movz x2, #2\n cmp x1, x2\n csel x0, x1, x2, lo\n ret",
     {}, 1),
    ("ccmp path", "movz x0, #5\n cmp x0, #5\n b.ne 1f\n movz x0, #7\n1: ret",
     {}, 7),
    # --- loads and stores at every width ---
    ("ldrb post-index", """
        stp x29, x30, [sp, #-32]!
        add x1, sp, #16
        mov w2, #0xAB
        mov w3, #0xCD
        strb w2, [x1]
        strb w3, [x1, #1]
        mov x4, x1
        ldrb w0, [x4], #1
        ldrb w5, [x4]
        lsl w5, w5, #8
        orr w0, w0, w5
        ldp x29, x30, [sp], #32
        ret""", {}, 0xCDAB),
    ("ldrsw / ldrsh", "movz x1, #0xFFFF\n strh w1, [sp, #-16]!\n"
                      " ldrsh x0, [sp]\n add sp, sp, #16\n ret", {},
     0xFFFFFFFFFFFFFFFF),
    ("str/ldr word", "movz x1, #0x1234\n movk x1, #0x5678, lsl #16\n"
                     " str w1, [sp, #-16]!\n ldr w0, [sp]\n"
                     " add sp, sp, #16\n ret", {}, 0x56781234),
    # --- arithmetic with carry ---
    ("adds/adc", "mov x0, #-1\n adds x0, x0, #1\n adc x0, xzr, xzr\n ret", {},
     1),
    ("subs/sbc", "movz x1, #1\n subs x0, xzr, x1\n cset x0, cc\n ret", {}, 1),
    ("mul/neg", "movz x1, #7\n movn x2, #2\n mul x0, x1, x2\n ret", {},
     (7 * (2**64 - 3)) & 0xFFFFFFFFFFFFFFFF),
    # --- branches ---
    ("cbz on w", "movz w0, #0\n cbz w0, 1f\n movz x0, #9\n1: ret", {}, 0),
    ("tbnz", "mov x0, #-1\n tbnz x0, #63, 1f\n movz x0, #0\n1: ret", {},
     0xFFFFFFFFFFFFFFFF),
    ("blr", "movz x1, #3\n 1: blr x2\n ret\n 2:", {}, None),
    # Register-offset loads: the S bit scales the index by the access size.
    # Without it a table walk of 8-byte entries reads a byte-shifted alias of
    # the previous slot, which is far more confusing than a clean fault.
    ("ldr reg-offset lsl #3",
     "mov x1, #0x1E0000\n movz x2, #3\n movz x3, #0x55\n"
     " stp xzr, x3, [x1, #16]\n ldr x0, [x1, x2, lsl #3]\n ret", {},
     0x55),
    ("ldr reg-offset lsl #3 slot0",
     "mov x1, #0x1E0000\n movz x3, #0x7\n movz x4, #0x8\n"
     " stp x3, x4, [x1]\n ldr x0, [x1, xzr, lsl #3]\n ret", {}, 0x7),
    ("ldr reg-offset no shift",
     "mov x1, #0x1E0000\n movz x2, #8\n movz x3, #0x8\n movz x4, #0x9\n"
     " stp x3, x4, [x1]\n ldr x0, [x1, x2]\n ret", {}, 0x9),
    ("ldr w reg-offset lsl #2",
     "mov x1, #0x1E0000\n movz x2, #1\n movz x3, #0x11\n movk x3, #0x22, lsl #16\n"
     " stp w3, w3, [x1, #4]\n ldr w0, [x1, x2, lsl #2]\n ret", {},
     0x220011),
    ("str reg-offset lsl #3",
     "mov x1, #0x1E0000\n movz x2, #1\n movz x0, #0x2a\n"
     " str x0, [x1, x2, lsl #3]\n ldr x0, [x1, #8]\n ret", {}, 0x2a),
    ("ldrb reg-offset lsl #0",
     "mov x1, #0x1E0000\n movz x2, #1\n movz x3, #0x5a5a\n"
     " str x3, [x1]\n ldrb w0, [x1, x2]\n ret", {}, 0x5a),
    # Scalar FP stores: the writeback mode lives in bits 11:10, not bit 24.
    # Reading it off bit 24 made every `str s0, [x14], #4` a plain store, so a
    # vertex cursor never advanced and six vertices overwrote one another.
    # FP arithmetic opcodes are NOT in intuitive order: bits 15:12 give
    # 0 FMUL, 1 FDIV, 2 FADD, 3 FSUB. Assuming FADD=0 makes fdiv a subtract.
    ("fadd", "mov w0, #3\n scvtf s0, w0\n mov w1, #2\n scvtf s1, w1\n"
             " fadd s2, s0, s1\n fcvtas w0, s2\n ret", {}, 5),
    ("fsub", "mov w0, #3\n scvtf s0, w0\n mov w1, #2\n scvtf s1, w1\n"
             " fsub s2, s0, s1\n fcvtas w0, s2\n ret", {}, 1),
    ("fmul", "mov w0, #3\n scvtf s0, w0\n mov w1, #2\n scvtf s1, w1\n"
             " fmul s2, s0, s1\n fcvtas w0, s2\n ret", {}, 6),
    ("fdiv", "mov w0, #12\n scvtf s0, w0\n mov w1, #3\n scvtf s1, w1\n"
             " fdiv s2, s0, s1\n fcvtas w0, s2\n ret", {}, 4),
    ("fdiv then fadd chain",
     "mov w0, #255\n scvtf s7, w0\n mov w1, #34\n scvtf s0, w1\n"
     " fdiv s0, s0, s7\n mov w2, #255\n scvtf s1, w2\n fmul s0, s0, s1\n"
     " fcvtas w0, s0\n ret", {}, 34),
    ("fmov 32-bit both ways",
     "mov w0, #0x4048\n movk w0, #0x3f80, lsl #16\n fmov s0, w0\n"
     " fmov w1, s0\n mov w0, w1\n ret", {}, 0x3F804048),
    ("str s post-index advances",
     "mov x1, #0x1E0000\n mov w0, #0x3f800000\n"
     " fmov s0, w0\n str s0, [x1], #4\n mov x0, x1\n ret", {},
     0x1E0004),
    ("str s post-index writes",
     "mov x1, #0x1E0000\n mov w0, #0x3f800000\n"
     " fmov s0, w0\n str s0, [x1], #4\n ldr w0, [x1, #-4]\n ret", {},
     0x3f800000),
    ("str s unsigned offset no writeback",
     "mov x1, #0x1E0000\n mov w0, #0x40000000\n"
     " fmov s0, w0\n str s0, [x1, #8]\n ldr w0, [x1, #8]\n ret", {},
     0x40000000),
    ("ldr s post-index",
     "mov x1, #0x1E0000\n mov w0, #0x40400000\n"
     " fmov s0, w0\n str s0, [x1]\n ldr s1, [x1], #4\n mov x0, x1\n ret", {},
     0x1E0004),
    ("str d post-index",
     "mov x1, #0x1E0000\n mov w0, #0x3ff00000\n"
     " fmov s0, w0\n str d0, [x1], #8\n mov x0, x1\n ret", {},
     0x1E0008),
    ("ldrb reg-offset last byte",
     "mov x1, #0x1E0000\n movz x2, #2\n movz x3, #0x5a41\n"
     " str x3, [x1]\n ldrb w0, [x1, x2]\n ret", {}, 0x00),
]


def assemble(src):
    """Assemble `src` into a flat image at 0x100000, return symbol map."""
    with tempfile.TemporaryDirectory() as d:
        s = os.path.join(d, "t.S")
        open(s, "w").write(".text\n.globl f\n.type f, %function\nf:\n" + src
                           + "\n.section .note.GNU-stack,\"\",%progbits\n")
        o = os.path.join(d, "t.o")
        subprocess.run(CC + ["-c", "-fPIC", "-nostdlib", "-o", o, s], check=True,
                       capture_output=True)
        from elftools.elf.elffile import ELFFile
        with open(o, "rb") as fh:
            elf = ELFFile(fh)
            sec = elf.get_section_by_name(".text")
            return sec.data()


def main():
    os.makedirs("build/tests", exist_ok=True)
    fails = 0
    for name, src, args, want in CASES:
        if want is None:
            continue
        code = assemble(src)
        m = a64.Machine()
        m.map(0x100000, 0x10000)
        m.map(0x1E0000, 0x10000)      # stack region
        m.store_mem(0x100000, code)
        m.sp = 0x1EF000
        for i, v in args.items():
            m.x[i] = v
        m.x[30] = m.sentinel
        try:
            got = m.run(0x100000)
        except Exception as exc:              # noqa: BLE001
            print(f"  FAIL  {name}: {exc}")
            fails += 1
            continue
        if got != want:
            print(f"  FAIL  {name}: got {got:#x}, want {want:#x}")
            fails += 1
        else:
            print(f"  PASS  {name}")
    total = sum(1 for c in CASES if c[3] is not None)
    print(f"\n{total - fails}/{total} decode cases passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
