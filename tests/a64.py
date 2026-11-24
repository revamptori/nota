#!/usr/bin/env python3
"""a64.py - a small AArch64 interpreter used by NOTA's test suite.

Why this exists
---------------
NOTA's logic is AArch64 assembly and the build sandbox has no Android device.
Unicorn's AArch64 core (2.x) mis-executes `ret`/`br`, so it cannot be used to
test anything with function calls. This interpreter implements the subset of
AArch64 emitted by NOTA's sources and by clang's code generation:

  data processing (immediate / shifted register / extended register)
  moves (movz/movn/movk, mov alias)
  loads/stores (scaled/unsigned immediate, pre/post-index, register offset,
                ldrb/ldrh/ldrsb/ldrsw/strb/strh, ldp/stp)
  PC-relative addressing (adr/adrp + add :lo12:)
  branches (b, bl, br, blr, ret, b.cond, cbz/cbnz, tbz/tbnz)
  division (sdiv/udiv), madd/msub, shifts (lsl/lsr/asr/ror), bit ops
  csel/csinc/csinv/csneg, ccmp, clz/rbit/rev, uxtb/uxth/sxtb/sxth/sxtw
  msr/mrs for the handful of system registers NOTA reads

Any illegal instruction raises IllegalInstruction with the PC, so a test that
touches unsupported code fails loudly rather than silently.

The interpreter is *only* used on the host; the APK runs the same bytes on a
real ARM64 core.
"""
import math
import struct

MASK64 = (1 << 64) - 1


class IllegalInstruction(Exception):
    pass


class MemFault(Exception):
    pass


def sx(v, bits):
    sign = 1 << (bits - 1)
    v &= (1 << bits) - 1
    return (v ^ sign) - sign


class Machine:
    def __init__(self, mem_size=(1 << 24)):
        self.x = [0] * 32
        # 32 SIMD/FP registers, kept as raw 128-bit ints so single- and
        # double-precision values can share the file. The renderer only uses
        # scalar singles, but storing raw bits means a stp of the q registers
        # would not have to know that.
        self.q = [0] * 32
        self.sp = 0
        self.n = False
        self.z = False
        self.c = False
        self.v = False
        self.pc = 0
        self.sentinel = 0xFFFFF000   # return address that ends run()
        self.mem = {}          # page(4096) -> bytearray
        self.insn_count = 0
        self.cycles = 0        # rough cost model: 1 per insn, +4 memory
        self.exit_pc = None
        self.syscall = None    # optional handler(machine) -> next pc
        self._cache = {}

    # ---------------------------------------------------------- memory ----
    def map(self, addr, size):
        page = addr & ~0xFFF
        end = addr + size
        while page < end:
            if page not in self.mem:
                self.mem[page] = bytearray(4096)
            page += 4096

    def load_mem(self, addr, size):
        out = bytearray()
        while size:
            page = addr & ~0xFFF
            off = addr & 0xFFF
            blk = self.mem.get(page)
            if blk is None:
                raise MemFault(f"unmapped read at {addr:#x}")
            n = min(size, 4096 - off)
            out += blk[off:off + n]
            addr += n
            size -= n
        return bytes(out)

    def store_mem(self, addr, data):
        i = 0
        while i < len(data):
            page = (addr + i) & ~0xFFF
            off = (addr + i) & 0xFFF
            blk = self.mem.get(page)
            if blk is None:
                raise MemFault(f"unmapped write at {addr + i:#x}")
            n = min(len(data) - i, 4096 - off)
            blk[off:off + n] = data[i:i + n]
            i += n

    # --------------------------------------------------------- helpers ----
    def reg(self, i):
        return 31 if False else self.x[i]

    def get(self, i, is_sp=False):
        if i == 31:
            return self.sp if is_sp else 0
        return self.x[i]

    def set(self, i, v, is_sp=False):
        v &= MASK64
        if i == 31:
            if is_sp:
                self.sp = v
        else:
            self.x[i] = v

    def getq(self, i):
        return self.q[i]

    def setq(self, i, v):
        self.q[i] = v & ((1 << 128) - 1)

    def gets(self, i):
        """A single-precision register as a Python float."""
        return struct.unpack("<f", struct.pack("<I", self.q[i] & 0xFFFFFFFF))[0]

    def sets(self, i, v):
        """Store a Python float, quietly normalising NaN/inf like the FPU."""
        try:
            bits = struct.unpack("<I", struct.pack("<f", v))[0]
        except OverflowError:
            bits = 0x7F800000 if v > 0 else 0xFF800000
        self.q[i] = (self.q[i] & ~0xFFFFFFFF) | bits

    def write_cstr(self, addr, s):
        self.store_mem(addr, s.encode() + b"\x00")

    def read_cstr(self, addr, limit=65536):
        out = bytearray()
        while len(out) < limit:
            b = self.load_mem(addr + len(out), 1)[0]
            if b == 0:
                break
            out.append(b)
        return bytes(out)

    # ----------------------------------------------------------- fetch ----
    def fetch(self):
        raw = self.load_mem(self.pc, 4)
        insn = struct.unpack("<I", raw)[0]
        return insn

    def run(self, pc, max_insns=5_000_000, stop_pc=None, lr=None):
        self.pc = pc
        self.x[30] = self.sentinel if lr is None else lr
        self.exit_pc = None
        stop_pc = self.sentinel if stop_pc is None else stop_pc
        return_status = None
        while True:
            if self.pc == stop_pc or self.pc == 0:
                return_status = self.x[0]
                break
            if self.insn_count > max_insns:
                raise IllegalInstruction(f"instruction budget exhausted at {self.pc:#x}")
            try:
                next_pc = self.step()
            except MemFault as exc:
                raise MemFault(f"{exc} (pc={self.pc:#x})") from None
            self.insn_count += 1
            self.pc = next_pc & MASK64
        self.exit_pc = self.pc
        return return_status

    # ------------------------------------------------------------ core ----
    def step(self):
        insn = self.fetch()
        # `svc #0` is the boundary between NOTA and the kernel. A Machine has a
        # syscall handler so tests can service file I/O from the host; without
        # one the trap is loud rather than silently wrong.
        if (insn & 0xFFE0001F) == 0xD4000001:
            if self.syscall is None:
                raise IllegalInstruction(
                    f"svc #0 at {self.pc:#x} but no syscall handler installed")
            return self.syscall(self)
        # (single-step; `run()` drives the PC)
        pc = self.pc
        handler = self._cache.get(insn)
        if handler is None:
            handler = self.decode(insn)
            self._cache[insn] = handler
        return handler(insn, pc)

    def decode(self, insn):
        """Return the executor for `insn` (also used to pre-validate code)."""
        return lambda i, pc: self.execute(i, pc)

    # ------------------------------------------------------- execution ----
    def execute(self, insn, pc):
        rd = insn & 0x1F
        rn = (insn >> 5) & 0x1F
        sf = (insn >> 31) & 1
        op0 = (insn >> 25) & 0xF

        # ---- branch / exception group (op0 == 0b101x) ----
        # b/bl: bits 31:26 == 0b100101 (B) or 0b100101 with bit31=1 (BL)
        if (insn & 0x7C000000) == 0x14000000:            # b / bl
            off = sx(insn & 0x03FFFFFF, 26) << 2
            if (insn >> 31) & 1:
                self.x[30] = pc + 4
            return pc + off

        if (insn & 0xFE000000) == 0xD6000000:            # br/blr/ret
            opc = (insn >> 21) & 0xF
            if opc == 0:                                  # br
                return self.get(rn)
            if opc == 1:                                  # blr
                self.x[30] = pc + 4
                return self.get(rn)
            if opc == 2:                                  # ret
                return self.get(rn)
            raise IllegalInstruction(f"branch opc {opc} at {pc:#x}")

        if (insn & 0xFF000010) == 0x54000000:            # b.cond
            cond = insn & 0xF
            off = sx((insn >> 5) & 0x7FFFF, 19) << 2
            return pc + off if self.cond(cond) else pc + 4

        if (insn & 0x7E000000) == 0x34000000:            # cbz/cbnz
            op = (insn >> 24) & 1
            off = sx((insn >> 5) & 0x7FFFF, 19) << 2
            # CBZ/CBNZ: Rt is bits 4:0 (not Rn), width is bit 31 (sf)
            wide = (insn >> 31) & 1
            rt = insn & 0x1F
            raw = self.get(rt)
            # In the 32-bit form only the low word is tested.
            val = raw if wide else (raw & 0xFFFFFFFF)
            taken = (val == 0) if op == 0 else (val != 0)
            return (pc + off if taken else pc + 4)

        if (insn & 0x7E000000) == 0x36000000:            # tbz/tbnz
            op = (insn >> 24) & 1
            b5 = (insn >> 31) & 1
            bit = ((insn >> 19) & 0x1F) | (b5 << 5)
            off = sx((insn >> 5) & 0x3FFF, 14) << 2
            val = self.get(rd)
            taken = not ((val >> bit) & 1) if op == 0 else bool((val >> bit) & 1)
            return pc + off if taken else pc + 4

        # ---- loads and stores ----
        # LDR/STR immediate forms. All share bits 29:27 == 111 with V (bit 26)
        # clear; the discriminator is bits 11:10, NOT bit 24 (bit 24 is only
        # set for the unsigned-offset form *when the offset is non-zero* -
        # [x1] and [x1,#0] encode as 0x39.. with bit 24 set but are otherwise
        # identical). Verified against the assembler:
        #   11:10 == 00 -> unsigned offset, 12-bit immediate scaled by access
        #   11:10 != 00 -> 9-bit signed immediate, mode 01 post / 11 pre /
        #                  10 unprivileged / 00 unscaled
        # Bit 24 selects the form: 1 = unsigned 12-bit scaled offset,
        # 0 = 9-bit signed immediate (unscaled / post / pre / unprivileged).
        # The size field (31:30) is independent of bit 24, so `strb [x1]`
        # (0x39000022) and `ldrb [x1],#1` (0x38401423) differ only there.
        if (insn & 0x3F000000) == 0x39000000:
            return self.ldst_imm(insn, pc)
        if (insn & 0x3F000000) == 0x38000000:
            # bit 21 distinguishes register-offset (0x382008xx) from imm9
            if (insn & 0x3B200C00) == 0x38200800:
                return self.ldst_reg(insn, pc)
            return self.ldst_prepost(insn, pc)
        # LDP/STP: bit 26 (V) must be 0 for general registers, and bits
        # 24:23 distinguish no-allocate / post-index / offset / pre-index.
        if (insn & 0x3A000000) == 0x28000000:
            return self.ldp_stp_offset(insn, pc)
        if (insn & 0x3B000000) == 0x18000000:            # LDR (literal)
            return self.ldr_literal(insn, pc)

        # ---- PC-relative ----
        # ADR  : op=0, immlo(31:30) immhi(23:5) Rd(4:0)  -> byte displacement
        # ADRP : op=1, same fields                        -> page displacement
        # The op bit is 31, so it cannot be masked out before being tested;
        # masking it away first made ADRP a displacement instruction with the
        # wrong shift, i.e. a plausible-looking wrong address.
        if (insn & 0x1F000000) == 0x10000000:            # adr/adrp
            imm = ((insn >> 5) & 0x7FFFF) << 2 | ((insn >> 29) & 3)
            imm = sx(imm, 21)
            if (insn >> 31) & 1:
                val = (pc & ~0xFFF) + (imm << 12)
            else:
                val = pc + imm
            self.set(rd, val)
            return pc + 4

        # ---- add/sub immediate ----
        if (insn & 0x1F000000) == 0x11000000:   # add/sub (imm), sf/op/S/imm12
            sh = (insn >> 22) & 1
            imm = (insn >> 10) & 0xFFF
            if sh:
                imm <<= 12
            sub = (insn >> 30) & 1
            sets = (insn >> 29) & 1
            a = self.get(rn, is_sp=True)
            b = imm
            if sub:
                res, carry, ovf = self.sub_flags(a, b, sf)
            else:
                res, carry, ovf = self.add_flags(a, b, sf)
            if sets:
                self.set_flags(res, carry, ovf, sf)
            # Add/sub *immediate*: Rd 31 means SP only when S == 0.
            self.set(rd, res, is_sp=(sets == 0))
            return pc + 4

        # ---- movz/movn/movk: sf | opc(2) | 100101 | hw(2) | imm16 | rd ----
        if (insn & 0x1F800000) == 0x12800000:
            return self.move_wide(insn, pc)

        # ---- logical immediate ----
        if (insn & 0x1F800000) == 0x12000000:
            return self.logic_imm(insn, pc)

        # ---- bitfield (sbfm/bfm/ubfm: lsl/lsr/asr/sxt/uxth/orr alias etc) ----
        if (insn & 0x1F800000) == 0x13000000:
            return self.bitfield(insn, pc)

        # ---- extract (ror alias) ----
        if (insn & 0x1F800000) == 0x13800000:
            return self.extract(insn, pc)

        # ---- data processing -- 2 source ----
        if (insn & 0x1FE00000) == 0x1AC00000:
            return self.dp2(insn, pc)

        # ---- data processing -- 1 source ----
        if (insn & 0x1FE00000) == 0x5AC00000:
            return self.dp1(insn, pc)

        # ---- add/sub shifted register ----
        if (insn & 0x1F200000) == 0x0B000000:
            return self.addsub_shifted(insn, pc)

        # ---- add/sub extended register ----
        if (insn & 0x1F200000) == 0x0B200000:
            return self.addsub_extended(insn, pc)

        # ---- logical shifted register ----
        if (insn & 0x1F000000) == 0x0A000000:
            return self.logic_shifted(insn, pc)

        # ---- add/sub with carry ----
        if (insn & 0x1FE00000) == 0x1A000000:
            return self.addsub_carry(insn, pc)

        # ---- conditional select ----
        if (insn & 0x1FE00000) == 0x1A800000:
            return self.cond_select(insn, pc)

        # ---- conditional compare ----
        if (insn & 0x1FE00800) in (0x1A400000, 0x1A400800):
            return self.cond_compare(insn, pc)

        # ---- madd/msub ----
        if (insn & 0x1F000000) == 0x1B000000:
            op = (insn >> 15) & 1
            ra = (insn >> 10) & 0x1F
            rm = (insn >> 16) & 0x1F
            prod = self.get(rn) * self.get(rm)
            acc = self.get(ra)
            self.set(rd, (acc - prod) if op else (acc + prod))
            return pc + 4


        # ---- scalar FP: conversion and arithmetic ----
        # The renderer only needs single precision, so this covers exactly the
        # operations src/tex.S and src/ui.S emit. Anything else stays
        # unsupported, which is the point: an unexercised instruction should
        # fail loudly rather than be modelled approximately.
        # Bit 21 is set for the whole scalar FP group. Within it, the two
        # families are told apart by bit 11: the arithmetic forms
        # (FMUL/FDIV/FADD/FSUB, and F*MADD which is not used here) have it
        # set, the conversion forms (SCVTF/UCVTF/FCVT*) do not. Splitting on
        # anything else - bit 12, or the 5-bit field at 16 - conflates the
        # two, which is how a first attempt produced silently zero results.
        # ---- FMOV between a general register and an FP register ----
        # `fmov wD, sN` / `fmov sD, wN` (sf=0) and the 64-bit pair (sf=1)
        # move bits, not values. The direction is bit 16 (part of the rmode
        # field): set is GP->FP (0x1E27..), clear is FP->GP (0x1E26..). The
        # mask must not include bits 15:10, which is where the two encodings
        # differ. Without this
        # a store of a constant float had to go through memory, which is
        # exactly the sort of detour that lets a decoder bug hide.
        # FMOV must be tested before the conversion family, but it also has to
        # be *narrow*: bits 15:10 are zero in both FMOV forms and non-zero for
        # SCVTF/FMUL, which share the same upper bits. A guard that ignored
        # them swallowed `scvtf s0, w0` and every int-to-float conversion
        # silently returned zero.
        # ftype (bits 23:22) is 0 for single and 1 for double, so it has to be
        # masked out; the other discriminators are bits 20:16 (6 = FP->GP,
        # 7 = GP->FP) and bits 15:10, which are zero in both FMOV forms.
        if ((insn & 0x7F200000) == 0x1E200000
                and ((insn >> 16) & 0x1F) in (6, 7)
                and ((insn >> 10) & 0x3F) == 0):
            sf = (insn >> 31) & 1
            rn_i = (insn >> 5) & 0x1F
            rd = insn & 0x1F
            width = 64 if sf else 32
            if ((insn >> 16) & 0x1F) == 7:             # GP -> FP
                v = self.get(rn_i) & ((1 << width) - 1)
                if sf:
                    self.setq(rd, v)               # fmov dD, xN replaces all 64
                else:
                    self.setq(rd, (self.q[rd] & ~0xFFFFFFFF) | v)
            else:                                      # FP -> GP (zero-extends)
                self.set(rd, self.getq(rn_i) & ((1 << width) - 1))
            return pc + 4

        if (insn & 0x5F200000) == 0x1E200000 and ((insn >> 21) & 1):
            ftype = (insn >> 22) & 3
            single = ftype == 0
            rn_i = (insn >> 5) & 0x1F
            rm_i = (insn >> 16) & 0x1F

            def freg(i):
                if single:
                    return self.gets(i)
                return struct.unpack("<d", struct.pack("<Q", self.getq(i)))[0]

            def fset(i, v):
                if single:
                    self.sets(i, v)
                else:
                    self.setq(i, struct.unpack("<Q", struct.pack("<d", v))[0])

            if (insn >> 11) & 1:
                # ---- arithmetic: Rm is a register ----
                a, b = freg(rn_i), freg(rm_i)
                # opcode is bits 15:12, verified against the assembler:
                #   0 FMUL, 1 FDIV, 2 FADD, 3 FSUB
                # (The intuitive FADD=0 ordering is wrong, and getting it wrong
                # turns a divide into a subtract - which produces a plausible
                # number rather than a fault.)
                op = (insn >> 12) & 0xF
                if op == 2:
                    r = a + b
                elif op == 3:
                    r = a - b
                elif op == 0:
                    r = a * b
                elif op == 1:
                    if b == 0:
                        r = (math.inf if a > 0 else
                             -math.inf if a < 0 else math.nan)
                    else:
                        r = a / b
                else:
                    raise IllegalInstruction(
                        f"unsupported FP arithmetic {insn:#010x} at {pc:#x}")
                fset(rd, r)
                return pc + 4

            # ---- conversion ----
            # The operation is bits 18:16 (3 bits); bits 20:19 are rmode and
            # must not be folded in. `scvtf s7, w7` is 0x1E2908E7: opcode 1
            # with rmode 1, so a 5-bit read at 20:16 yields 9 and matches
            # nothing, which is how every int-to-float conversion silently
            # produced zero.
            opcode = (insn >> 16) & 7
            rmode = (insn >> 19) & 3
            if opcode in (2, 3):                     # SCVTF / UCVTF
                sf = (insn >> 31) & 1
                raw = self.get(rn_i)
                if not sf:
                    raw &= 0xFFFFFFFF
                if opcode == 2:                      # signed
                    if sf and raw >= (1 << 63):
                        raw -= 1 << 64
                    elif not sf and raw >= (1 << 31):
                        raw -= 1 << 32
                fset(rd, float(raw))
                return pc + 4
            if opcode in (0, 1, 4, 5):               # FCVTN*/FCVTA*/FCVTZ*
                v = freg(rn_i)
                if opcode in (0, 1):                 # FCVTNS / FCVTNU round-to-nearest
                    r = math.floor(v + 0.5) if v >= 0 else math.ceil(v - 0.5)
                elif opcode in (4, 5):               # FCVTAS / FCVTAU
                    r = math.floor(v + 0.5) if v >= 0 else math.ceil(v - 0.5)
                else:                                # FCVTZS / FCVTZU truncate
                    r = math.trunc(v)
                sf = (insn >> 31) & 1
                if opcode in (1, 5) and r < 0:       # unsigned forms
                    r &= (1 << (64 if sf else 32)) - 1
                self.set(rd, r)
                return pc + 4

            raise IllegalInstruction(
                f"unsupported FP conversion {insn:#010x} at {pc:#x}")


        # ---- scalar FP load/store (stur/ldur/str/ldr s/d) ----
        # Same addressing forms as the general-purpose versions, but bit 26
        # selects the SIMD&FP register file.
        # bit 24 distinguishes the unsigned-offset form (no writeback) from
        # the imm9 forms, exactly as for the integer load/store group.
        # 0x3C.. is post-index/pre-index/unscaled, 0x3D.. is unsigned offset
        if (insn & 0x3E000000) == 0x3C000000:
            size = (insn >> 30) & 3          # 0 = 8-bit, 2 = 32-bit, 3 = 64
            opc = (insn >> 22) & 3
            is_load = bool(opc & 1)
            # bits 25:24 == 01 is the unsigned-offset form; the imm9 forms are
            # bits 25:24 == 00 and select unscaled / post / unprivileged / pre
            # with bits 11:10. Testing bit 24 alone made every post-index
            # `str s0, [x14], #4` a no-writeback store, so a vertex-buffer
            # cursor never advanced and all six vertices landed on top of
            # each other.
            unsigned = ((insn >> 24) & 3) == 1
            if unsigned:
                # imm12 is scaled by the access size, as for the integer
                # forms: `ldr s0,[x9,#0x40]` carries 0x10, and the hardware
                # multiplies by 4. Verified against the assembler.
                imm9 = ((insn >> 10) & 0xFFF) * (4 if size == 2 else 8)
                mode = 2                     # no writeback
            else:
                imm9 = sx((insn >> 12) & 0x1FF, 9)
                mode = (insn >> 10) & 3      # 0 unscaled, 1 post, 3 pre
            rn_i = (insn >> 5) & 0x1F
            rt = insn & 0x1F
            base = self.get(rn_i, is_sp=True)
            addr = base if mode == 1 else base + imm9
            nbytes = 4 if size == 2 else 8
            if is_load:
                raw = int.from_bytes(self.load_mem(addr, nbytes), "little")
                if size == 2:
                    self.setq(rt, (self.q[rt] & ~0xFFFFFFFF) | raw)
                else:
                    self.setq(rt, raw)
            else:
                bits = self.q[rt] & ((1 << (nbytes * 8)) - 1)
                self.store_mem(addr, bits.to_bytes(nbytes, "little"))
            if mode == 1:
                self.set(rn_i, base + imm9, is_sp=True)
            elif mode == 3:
                self.set(rn_i, base + imm9, is_sp=True)
            return pc + 4

        # ---- nop / hints ----
        if (insn & 0xFFFFF01F) == 0xD503201F:
            return pc + 4
        if insn == 0xD5033FDF:  # isb
            return pc + 4
        if insn == 0xD5033F9F:  # dsb
            return pc + 4
        if insn == 0xD5033FBF:  # dmb
            return pc + 4

        raise IllegalInstruction(
            f"unsupported instruction {insn:#010x} at {pc:#x}")

    # ------------------------------------------------------------ flags ----
    def cond(self, c):
        n, z, cc, v = self.n, self.z, self.c, self.v
        return [
            z, not z, cc, not cc, n, not n, v, not v,
            cc and not z, (not cc) or z, n == v, n != v,
            (not z) and (n == v), z or (n != v), True, True,
        ][c]

    def set_flags(self, res, carry, ovf, sf):
        sign = 1 << (63 if sf else 31)
        res &= MASK64 if sf else 0xFFFFFFFF
        self.n = bool(res & sign)
        self.z = (res & (MASK64 if sf else 0xFFFFFFFF)) == 0
        self.c = carry
        self.v = ovf

    def add_flags(self, a, b, sf):
        width = 64 if sf else 32
        mask = (1 << width) - 1
        a &= mask
        b &= mask
        r = a + b
        carry = r > mask
        res = r & mask
        ovf = ((a ^ res) & (b ^ res) & (1 << (width - 1))) != 0
        return res, carry, ovf

    def sub_flags(self, a, b, sf):
        width = 64 if sf else 32
        mask = (1 << width) - 1
        a &= mask
        b &= mask
        r = a - b
        carry = a >= b
        res = r & mask
        ovf = ((a ^ b) & (a ^ res) & (1 << (width - 1))) != 0
        return res, carry, ovf

    # ---------------------------------------------------------- shapes ----
    def ext(self, val, sf):
        return val & (MASK64 if sf else 0xFFFFFFFF)

    def ldst_imm(self, insn, pc):
        size = (insn >> 30) & 3
        opc = (insn >> 22) & 3
        rn = (insn >> 5) & 0x1F
        rt = insn & 0x1F
        imm = ((insn >> 10) & 0xFFF) << size
        base = self.get(rn, is_sp=True)
        addr = base + imm
        width = 1 << size                # bytes accessed
        # opc: 00 store, 01 load, 10 signed load (LDRSB/LDRSH/LDRSW),
        # 11 privileged. Only opc == 0 stores.
        if opc != 0:
            v = int.from_bytes(self.load_mem(addr, width), "little")
            if opc == 2 and size != 3:            # signed loads
                v = sx(v, width * 8)
            self.bytes = self.load_mem(addr, width)
            self.set(rt, v)
            self.cycles += 4
        else:                            # store
            v = self.get(rt)
            self.store_mem(addr, (v & ((1 << (width * 8)) - 1))
                           .to_bytes(width, "little"))
            self.cycles += 4
        return pc + 4

    def ldst_prepost(self, insn, pc):
        """LDR/STR (immediate, 9-bit signed): unrestricted, post-index,
        pre-index and unscaled all share this encoding."""
        size = (insn >> 30) & 3
        opc = (insn >> 22) & 3
        mode = (insn >> 10) & 3          # 00 unscaled, 01 post, 10 unpriv, 11 pre
        rn = (insn >> 5) & 0x1F
        rt = insn & 0x1F
        imm = sx((insn >> 12) & 0x1FF, 9)
        base = self.get(rn, is_sp=True)
        # mode: 00 unscaled (LDUR/STUR), 01 post-index, 10 unprivileged, 11 pre.
        # Only post-index reads the base itself; everything else adds imm.
        addr = base if mode == 1 else base + imm
        width = 1 << size                # bytes accessed (not destination size)
        # opc: 00 = store, 01 = load, 10 = signed load (or STTR when the
        # mode field says unprivileged), 11 = LDRSW. Only opc == 0 stores.
        if opc != 0 and not (mode == 2 and opc == 2):
            raw = int.from_bytes(self.load_mem(addr, width), "little")
            if opc in (2, 3) and size != 3:   # LDRSB / LDRSH
                raw = sx(raw, width * 8)
            self.set(rt, raw)
            self.cycles += 4
        else:                            # store
            self.store_mem(addr, (self.get(rt) & ((1 << (width * 8)) - 1))
                           .to_bytes(width, "little"))
            self.cycles += 4
        if mode in (1, 3):               # write-back
            self.set(rn, base + imm, is_sp=True)
        return pc + 4

    def ldst_reg(self, insn, pc):
        size = (insn >> 30) & 3
        opc = (insn >> 22) & 3
        rn = (insn >> 5) & 0x1F
        rt = insn & 0x1F
        rm = (insn >> 16) & 0x1F
        # option (15:13) and S (12) scale/expand the index register. Ignoring
        # them makes every `ldr xN, [xBase, xIdx, lsl #3]` read from xBase +
        # xIdx, i.e. a byte-shifted alias of the intended slot - which is how a
        # pointer read out of a table of 8-byte entries came back shifted by
        # one byte.
        opt = (insn >> 13) & 7
        s_bit = (insn >> 12) & 1
        base = self.get(rn, is_sp=True)
        idx = self.get(rm)
        if opt == 0b010:                 # UXTW
            idx &= 0xFFFFFFFF
        elif opt == 0b011:               # LSL (64-bit index)
            pass
        elif opt == 0b110:               # SXTW
            idx = sx(idx, 32)
        elif opt == 0b111:               # SXTX
            idx = sx(idx, 64)
        else:
            raise IllegalInstruction(f"ldst reg option {opt:#b}")
        idx = (idx << (size if s_bit else 0)) & MASK64
        addr = (base + idx) & MASK64
        width = 1 << size
        if opc & 1:
            raw = int.from_bytes(self.load_mem(addr, width), "little")
            if opc & 2:
                raw = sx(raw, width * 8)
            self.set(rt, raw)
        else:
            self.store_mem(addr, (self.get(rt) & ((1 << (width * 8)) - 1)).to_bytes(width, "little"))
        return pc + 4

    def ldp_stp_offset(self, insn, pc):
        opc = (insn >> 30) & 3
        mode = (insn >> 23) & 3
        rt2 = (insn >> 10) & 0x1F
        rn = (insn >> 5) & 0x1F
        rt = insn & 0x1F
        imm = sx((insn >> 15) & 0x7F, 7)
        # imm7 is *always* scaled by the access size, in every mode: the
        # assembler encodes [sp,#16] as imm7=2 for a 64-bit pair, and
        # [sp,#-32]! as imm7=124 (= -4 * 8).
        imm <<= 2 if opc == 0 else 3
        if opc == 1:                 # LDPSW: signed words, 8-byte step
            imm <<= 1
        base = self.get(rn, is_sp=True)
        # mode bits (24:23), verified against the assembler:
        #   01 = post-index, 10 = signed offset, 11 = pre-index. NOTA never
        # emits 00 (non-temporal pair), so it is rejected rather than guessed.
        if mode == 0:
            raise IllegalInstruction("non-temporal ldp/stp unsupported")
        # post-index accesses the base address and writes back afterwards;
        # offset and pre-index both access base + imm (only pre-index writes
        # back, which _ldp_stp_body handles).
        addr = base if mode == 1 else base + imm
        # `imm` must stay the scaled offset for the write-back below
        return self._ldp_stp_body(insn, pc, opc, mode, rt, rt2, rn, base,
                                  addr, imm)

    def _ldp_stp_body(self, insn, pc, opc, mode, rt, rt2, rn, base, addr, imm=0):
        width = 4 if opc == 0 else 8
        if (insn >> 26) & 1:            # V bit: SIMD, not used by NOTA
            raise IllegalInstruction("SIMD ldp/stp unsupported")
        if (insn >> 22) & 1:  # load
            a = int.from_bytes(self.load_mem(addr, width), "little")
            b = int.from_bytes(self.load_mem(addr + width, width), "little")
            if opc == 1:
                a, b = sx(a, 32), sx(b, 32)
            self.set(rt, a)
            self.set(rt2, b)
            self.cycles += 8
        else:
            self.store_mem(addr, (self.get(rt) & ((1 << (width * 8)) - 1)).to_bytes(width, "little"))
            self.store_mem(addr + width,
                           (self.get(rt2) & ((1 << (width * 8)) - 1)).to_bytes(width, "little"))
            self.cycles += 8
        if mode == 1:                 # post-index
            self.set(rn, base + imm, is_sp=True)
        elif mode == 3:               # pre-index
            self.set(rn, addr, is_sp=True)
        return pc + 4

    def ldr_literal(self, insn, pc):
        opc = (insn >> 30) & 3
        rt = insn & 0x1F
        imm = sx((insn >> 5) & 0x7FFFF, 19) << 2
        addr = pc + imm
        if opc == 0:
            v = int.from_bytes(self.load_mem(addr, 4), "little")
        elif opc == 1:
            v = int.from_bytes(self.load_mem(addr, 8), "little")
        elif opc == 2:
            v = sx(int.from_bytes(self.load_mem(addr, 4), "little"), 32)
        else:
            raise IllegalInstruction("prfm")
        self.set(rt, v)
        return pc + 4

    # ------------------------------------------------------- arithmetic ----
    def addsub_shifted(self, insn, pc):
        return self._addsub(insn, pc, extended=False)

    def addsub_extended(self, insn, pc):
        return self._addsub(insn, pc, extended=True)

    def _addsub(self, insn, pc, extended):
        sf = (insn >> 31) & 1
        sub = (insn >> 30) & 1
        sets = (insn >> 29) & 1
        shift_type = (insn >> 22) & 3
        rm = (insn >> 16) & 0x1F
        imm6 = (insn >> 10) & 0x3F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        a = self.get(rn)
        b = self.get(rm)
        if extended:
            opt = (insn >> 13) & 7
            sh = (insn >> 10) & 1
            b = self.extend_reg(b, opt, sf, sh)
            if opt in (3, 7):   # sp/zero variants keep 64-bit
                a = self.get(rn, is_sp=True)
        else:
            b = self.shift(b, shift_type, imm6, sf)
        if sub:
            res, carry, ovf = self.sub_flags(a, b, sf)
        else:
            res, carry, ovf = self.add_flags(a, b, sf)
        if sets:
            self.set_flags(res, carry, ovf, sf)
        self.set(rd, res)
        return pc + 4

    def extend_reg(self, v, opt, sf, sh):
        widths = {0: 8, 1: 16, 2: 32, 3: 64, 4: 8, 5: 16, 6: 32, 7: 64}
        w = widths[opt]
        if opt < 4:      # unsigned
            v &= (1 << w) - 1 if w < 64 else MASK64
        else:
            v = sx(v, w)
        if sh:
            v <<= w
        return v & MASK64

    def shift(self, v, t, amt, sf):
        width = 64 if sf else 32
        mask = (1 << width) - 1
        v &= mask
        amt &= width - 1
        if t == 0:
            return (v << amt) & mask
        if t == 1:
            return (v >> amt) & mask
        if t == 2:
            return (sx(v, width) >> amt) & mask
        if amt == 0:
            return v
        return ((v >> amt) | (v << (width - amt))) & mask

    def logic_shifted(self, insn, pc):
        sf = (insn >> 31) & 1
        opc = (insn >> 29) & 3
        shift_type = (insn >> 22) & 3
        # N (bit 21) inverts the shifted operand: that is what turns
        # ORR/BIC/EOR/ANDS into ORN/BIC-inverted/EON/BICS and `mvn`.
        invert = (insn >> 21) & 1
        rm = (insn >> 16) & 0x1F
        imm6 = (insn >> 10) & 0x3F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        a = self.get(rn)
        b = self.shift(self.get(rm), shift_type, imm6, sf)
        if invert:
            b = ~b & (MASK64 if sf else 0xFFFFFFFF)
        if opc == 3:                    # ANDS: sets flags, Rd may be XZR
            res = a & b
            self.set_flags(res, False, False, sf)
            self.set(rd, res)
            return pc + 4
        res = self._logic(opc, a, b, sf)
        self.set(rd, res)
        return pc + 4

    def _logic(self, opc, a, b, sf):
        mask = MASK64 if sf else 0xFFFFFFFF
        a &= mask
        b &= mask
        if opc == 0:
            return a & b
        if opc == 1:
            return a | b
        if opc == 2:
            return a ^ b
        return a & b & mask

    def logic_imm(self, insn, pc):
        sf = (insn >> 31) & 1
        opc = (insn >> 29) & 3
        n = (insn >> 22) & 1
        immr = (insn >> 16) & 0x3F
        imms = (insn >> 10) & 0x3F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        width = 64 if sf else 32
        imm = self.decode_bitmask(n, immr, imms, width)
        a = self.get(rn)
        if opc == 3:  # ANDS
            res, _, _ = self.add_flags(0, a & imm, sf) if False else (a & imm, False, False)
            self.set_flags(a & imm, False, False, sf)
            self.set(rd, a & imm)
            return pc + 4
        if opc == 0:
            res = a & imm
        elif opc == 1:
            res = a | imm
        else:
            res = a ^ imm
        self.set(rd, res)
        return pc + 4

    @staticmethod
    def decode_bitmask(n, immr, imms, width):
        combined = (n << 6) | (~imms & 0x3F)
        if combined == 0:
            return 0
        length = combined.bit_length() - 1
        levels = (1 << length) - 1
        s = imms & levels
        r = immr & levels
        if s == levels:
            raise IllegalInstruction("reserved bitmask")
        esize = 1 << length
        pattern = (1 << (s + 1)) - 1
        # ror by r within esize
        r %= esize
        pattern = ((pattern >> r) | (pattern << (esize - r))) & ((1 << esize) - 1)
        out = pattern          # replicate the element across the full width
        step = esize
        while step < width:
            out |= pattern << step
            step += esize
        return out & (MASK64 if width == 64 else 0xFFFFFFFF)

    def move_wide(self, insn, pc):
        sf = (insn >> 31) & 1
        opc = (insn >> 29) & 0x3          # MoveWideOp: 00 movn, 10 movz, 11 movk
        hw = (insn >> 21) & 3
        imm16 = (insn >> 5) & 0xFFFF
        rd = insn & 0x1F
        shift = hw << 4
        if opc == 2:                       # movz
            v = imm16 << shift
        elif opc == 0:                     # movn
            v = (~(imm16 << shift)) & MASK64
        elif opc == 3:                     # movk
            v = (self.get(rd) & ~(0xFFFF << shift)) | (imm16 << shift)
        else:
            raise IllegalInstruction("movw opc=01")
        self.set(rd, v)
        return pc + 4

    def bitfield(self, insn, pc):
        """SBFM (opc 0), BFM (opc 1), UBFM (opc 2).

        The bespoke per-alias version this replaces got ubfx/sxtb/lsr wrong.
        The definitions below are the canonical ones - note that the two
        cases (imms >= immr vs imms < immr) are what make `lsr` and `lsl`
        out of the same instruction:
          imms >= immr:  field starts at immr, width imms-immr+1
          imms <  immr:  field is placed at width-immr, width imms+1
        """
        sf = (insn >> 31) & 1
        opc = (insn >> 29) & 3
        n = (insn >> 22) & 1
        immr = (insn >> 16) & 0x3F
        imms = (insn >> 10) & 0x3F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        width = 64 if sf else 32
        mask = (1 << width) - 1
        src = self.get(rn) & mask
        if n != sf:
            raise IllegalInstruction(f"bitfield N={n} sf={sf} mismatch")
        if opc == 2 or opc == 0:                 # UBFM / SBFM
            if imms >= immr:
                w = imms - immr + 1
                v = (src >> immr) & ((1 << w) - 1)
            else:
                shift = width - immr
                w = imms + 1
                v = (src << shift) & ((1 << w) - 1)
            if opc == 0:                          # sign extend from bit w-1
                if (v >> (w - 1)) & 1:
                    res = (mask ^ ((1 << w) - 1)) | v
                else:
                    res = v
            else:
                res = v
        elif opc == 1:                            # BFM
            dst = self.get(rd) & mask
            if imms >= immr:                      # BFXIL
                keep = (1 << (imms + 1)) - 1
                res = (dst & ~keep & mask) | (src & keep)
            else:                                 # BFI / BFXIL
                shift = width - immr
                bits = (1 << (imms + 1)) - 1
                res = (dst & ~(bits << shift) & mask) | ((src & bits) << shift)
        else:
            raise IllegalInstruction(f"bitfield opc {opc}")
        self.set(rd, res & mask)
        return pc + 4

    def extract(self, insn, pc):
        sf = (insn >> 31) & 1
        rm = (insn >> 16) & 0x1F
        imms = (insn >> 10) & 0x3F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        width = 64 if sf else 32
        wmask = (1 << width) - 1
        a = self.get(rn) & wmask
        b = self.get(rm) & wmask
        if imms == 0:
            res = b
        else:
            res = ((b >> imms) | (a << (width - imms))) & wmask
        self.set(rd, res)
        return pc + 4

    def dp2(self, insn, pc):
        sf = (insn >> 31) & 1
        opcode = (insn >> 10) & 0x3F
        rm = (insn >> 16) & 0x1F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        a = self.get(rn)
        b = self.get(rm)
        # DP 2-source opcodes: 2 = UDIV, 3 = SDIV (unsigned and signed are
        # distinct encodings, not one encoding with a flag).
        if opcode in (0x02, 0x03):
            if b == 0:
                self.set(rd, 0)          # ARM defines division by zero as 0
                return pc + 4
            mask = MASK64 if sf else 0xFFFFFFFF
            if opcode == 0x02:           # UDIV
                q = (a & mask) // (b & mask)
            else:                        # SDIV, truncating toward zero
                av = sx(a, 64 if sf else 32)
                bv = sx(b, 64 if sf else 32)
                q = abs(av) // abs(bv)
                if (av < 0) != (bv < 0):
                    q = -q
            self.set(rd, q & mask)
            return pc + 4
        if opcode == 0x08:     # lslv
            amt = b & (63 if sf else 31)
            self.set(rd, self.shift(a, 0, amt, sf))
        elif opcode == 0x09:   # lsrv
            amt = b & (63 if sf else 31)
            self.set(rd, self.shift(a, 1, amt, sf))
        elif opcode == 0x0A:   # asrv
            amt = b & (63 if sf else 31)
            self.set(rd, self.shift(a, 2, amt, sf))
        elif opcode == 0x0B:   # rorv
            amt = b & (63 if sf else 31)
            self.set(rd, self.shift(a, 3, amt, sf))
        elif opcode == 0x0C:   # sdiv? no: udiv/ sdiv handled
            self.set(rd, a)
        elif opcode == 0x1A:   # sdiv
            pass
        else:
            raise IllegalInstruction(f"dp2 opcode {opcode:#x} at {pc:#x}")
        return pc + 4

    def dp1(self, insn, pc):
        sf = (insn >> 31) & 1
        opcode2 = (insn >> 16) & 0x1F
        opcode = (insn >> 10) & 0x3F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        width = 64 if sf else 32
        v = self.get(rn) & ((1 << width) - 1)
        if opcode == 4:        # rev / rbit / clz
            if opcode2 == 0:   # rbit
                r = int(f"{v:0{width}b}"[::-1], 2)
            elif opcode2 == 1: # rev16
                b = v.to_bytes(width // 8, "little")
                r = int.from_bytes(b"".join(b[i:i+2][::-1] for i in range(0, len(b), 2)), "little")
            elif opcode2 == 2: # rev32 / rev
                b = v.to_bytes(width // 8, "little")
                r = int.from_bytes(b"".join(b[i:i+4][::-1] for i in range(0, len(b), 4)), "little")
            else:              # rev64
                b = v.to_bytes(width // 8, "little")
                r = int.from_bytes(b[::-1], "little")
            self.set(rd, r)
            return pc + 4
        if opcode == 0x10 and opcode2 == 0:  # clz
            self.set(rd, width - v.bit_length() if v else width)
            return pc + 4
        if opcode == 0x10 and opcode2 == 1:  # cls
            bits = f"{v:0{width}b}"
            i = bits.find("1")
            self.set(rd, (bits[:i].count("0")) if i >= 0 else width)
            return pc + 4
        raise IllegalInstruction(f"dp1 opcode {opcode:#x}/{opcode2} at {pc:#x}")

    def addsub_carry(self, insn, pc):
        sf = (insn >> 31) & 1
        sub = (insn >> 30) & 1
        sets = (insn >> 29) & 1
        rm = (insn >> 16) & 0x1F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        c = 1 if self.c else 0
        a = self.get(rn)
        b = self.get(rm)
        if sub:
            res, carry, ovf = self.sub_flags(a, b + (1 - c) * 0 + (0 if c else 1), sf)
        else:
            res, carry, ovf = self.add_flags(a, b + c, sf)
        if sets:
            self.set_flags(res, carry, ovf, sf)
        self.set(rd, res)
        return pc + 4

    def cond_select(self, insn, pc):
        sf = (insn >> 31) & 1
        op = (insn >> 30) & 1
        op2 = (insn >> 10) & 3
        rm = (insn >> 16) & 0x1F
        cond = (insn >> 12) & 0xF
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        width = 64 if sf else 32
        mask = (1 << width) - 1
        a = self.get(rn) & mask
        b = self.get(rm) & mask
        if op2 == 0:            # csel
            val = a if self.cond(cond) else b
        elif op2 == 1:          # csinc
            val = a if self.cond(cond) else (b + 1)
        elif op2 == 2:          # csinv
            val = a if self.cond(cond) else (~b)
        else:                   # csneg
            val = a if self.cond(cond) else (-b)
        self.set(rd, val & mask)
        return pc + 4

    def cond_compare(self, insn, pc):
        sf = (insn >> 31) & 1
        sub = (insn >> 30) & 1
        imm_form = (insn >> 11) & 1
        cond = (insn >> 12) & 0xF
        rn = (insn >> 5) & 0x1F
        rd_nzcv = insn & 0xF
        a = self.get(rn)
        b = (insn >> 16) & 0x1F if imm_form == 0 else (insn >> 16) & 0x1F
        if imm_form:
            b = (insn >> 16) & 0x1F
        else:
            b = self.get((insn >> 16) & 0x1F)
        if self.cond(cond):
            res, carry, ovf = (self.sub_flags(a, b, sf) if sub
                               else self.add_flags(a, b, sf))
            self.set_flags(res, carry, ovf, sf)
        else:
            nzcv = rd_nzcv
            self.n = bool(nzcv & 8)
            self.z = bool(nzcv & 4)
            self.c = bool(nzcv & 2)
            self.v = bool(nzcv & 1)
        return pc + 4
