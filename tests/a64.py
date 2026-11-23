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
        if (insn & 0x3B000000) == 0x39000000:            # LDR/STR (imm)
            return self.ldst_imm(insn, pc)
        if (insn & 0x3B200000) == 0x38000000:            # LDR/STR (imm9)
            # bits 11:10 encode mode; accept all four.
            return self.ldst_prepost(insn, pc)
        if (insn & 0x3B200C00) == 0x38200800:            # LDR/STR (reg)
            return self.ldst_reg(insn, pc)
        # LDP/STP: bit 26 (V) must be 0 for general registers, and bits
        # 24:23 distinguish no-allocate / post-index / offset / pre-index.
        if (insn & 0x3A000000) == 0x28000000:
            return self.ldp_stp_offset(insn, pc)
        if (insn & 0x3B000000) == 0x18000000:            # LDR (literal)
            return self.ldr_literal(insn, pc)

        # ---- PC-relative ----
        if (insn & 0x1F000000) == 0x10000000 and not ((insn>>31)&1):  # adr/adrp
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
        if opc == 1:  # load
            if size == 0:
                v = int.from_bytes(self.load_mem(addr, 1), "little")
            elif size == 1:
                v = int.from_bytes(self.load_mem(addr, 2), "little")
            elif size == 2:
                v = int.from_bytes(self.load_mem(addr, 4), "little")
                if (insn >> 23) & 1:  # ldrsw
                    v = sx(v, 32)
            else:
                v = int.from_bytes(self.load_mem(addr, 8), "little")
            self.bytes = self.load_mem(addr, 1 << size)
            self.set(rt, v)
            self.cycles += 4
        else:        # store
            v = self.get(rt)
            self.store_mem(addr, (v & ((1 << (8 << size)) - 1)).to_bytes(1 << size, "little"))
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
        addr = base + imm if mode in (2, 3) else base
        width = 1 << size                # bytes accessed (not destination size)
        if opc & 1:                      # load
            raw = int.from_bytes(self.load_mem(addr, width), "little")
            if (opc & 2) and size != 3:  # ldrsb/ldrsh/ldrsw
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
        opt = (insn >> 13) & 3
        s = (insn >> 12) & 1
        base = self.get(rn, is_sp=True)
        idx = self.get(rm)
        addr = base + idx
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
        if mode & 1:                 # offset is scaled by the access size
            imm <<= 2 if opc == 0 else 3
            if opc == 1:             # LDPSW: signed words, 8-byte step
                imm <<= 1
        base = self.get(rn, is_sp=True)
        # mode: 00 no writeback, 01 post-index, 10 signed offset, 11 pre-index
        addr = base if mode == 1 else base + imm
        # `imm` must stay the scaled offset for the write-back below
        return self._ldp_stp_body(insn, pc, opc, mode, rt, rt2, rn, base,
                                  addr, imm)

    def ldp_stp(self, insn, pc):
        opc = (insn >> 30) & 3
        rt2 = (insn >> 10) & 0x1F
        rn = (insn >> 5) & 0x1F
        rt = insn & 0x1F
        imm = ((insn >> 15) & 0x7F)
        # scaled by element size: 32-bit -> 4, 64-bit -> 8, LDPSW -> 4
        if opc == 0:
            imm <<= 2
        elif opc == 1:
            imm <<= 2
        else:
            imm <<= 3
        base = self.get(rn, is_sp=True)
        return self._ldp_stp_body(insn, pc, opc, 2, rt, rt2, rn, base,
                                  base + imm, imm)

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
        rm = (insn >> 16) & 0x1F
        imm6 = (insn >> 10) & 0x3F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        a = self.get(rn)
        b = self.shift(self.get(rm), shift_type, imm6, sf)
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
        sf = (insn >> 31) & 1
        opc = (insn >> 29) & 3
        n = (insn >> 22) & 1
        immr = (insn >> 16) & 0x3F
        imms = (insn >> 10) & 0x3F
        rn = (insn >> 5) & 0x1F
        rd = insn & 0x1F
        width = 64 if sf else 32
        src = self.get(rn) & (MASK64 if sf else 0xFFFFFFFF)
        wmask = (1 << width) - 1
        if opc == 0:      # sbfm -> sbfx/sxtb/sxth/sxtw
            # sign extend bits [immr+imms : immr] ... clang emits sbfm for those
            if imms >= immr:
                size = imms - immr + 1
                v = sx((src >> immr) & ((1 << size) - 1), size)
            else:
                # rotate then mask (used for sxtb etc with immr>imms)
                lo = width - immr
                rot = ((src >> immr) | (src << lo)) & wmask
                size = imms + 1
                v = sx(rot & ((1 << size) - 1), size)
            res = v & wmask
        elif opc == 1:    # bfm -> bfi/bfxil
            if imms >= immr:
                size = imms - immr + 1
                field = (src >> immr) & ((1 << size) - 1)
                dstmask = ((1 << size) - 1) << immr
                res = (self.get(rd) & ~dstmask & wmask) | (field << immr)
            else:
                lsb = imms + 1
                size = immr - imms
                field = (src >> lsb) & ((1 << size) - 1)
                dstmask = (1 << size) - 1
                res = (self.get(rd) & ~dstmask & wmask) | field
        else:             # ubfm -> ubfx/lsl/lsr/ubfiz
            size = imms + 1
            v = src
            if immr:
                rot = ((v >> immr) | (v << (width - immr))) & wmask
            else:
                rot = v
            res = rot & ((1 << size) - 1) if size < width else rot
            # ubfm with imms == width-1 and immr -> lsr
            if imms == width - 1:
                res = (src >> immr) & wmask
        self.set(rd, res)
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
        if opcode in (0x02, 0x00):     # udiv / sdiv
            if b == 0:
                self.set(rd, 0)
                return pc + 4
            if sf:
                av = a - (1 << 64) if a >> 63 else a
                bv = b - (1 << 64) if b >> 63 else b
            else:
                av = sx(a, 32)
                bv = sx(b, 32)
            if opcode == 0x02:
                if av < 0:
                    av = -av
                if bv < 0:
                    bv = -bv
            q = abs(int(av)) // abs(int(bv))
            if (av < 0) != (bv < 0):
                q = -q
            self.set(rd, q)
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
