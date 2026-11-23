#!/usr/bin/env python3
"""harness.py - run NOTA's AArch64 assembly routines on an emulated CPU.

The sandbox has no Android device, so unit tests execute real AArch64 machine
code with Unicorn (QEMU's CPU core) against a small flat memory model:

  code   0x00100000..
  stack  0x00200000..  (grows down, SP starts at 0x002F0000)
  heap   0x01000000..
  data   0x02000000..  (literal strings / buffers)

C library calls a routine makes (malloc, open, ...) are *not* stubbed: tests
only cover routines that are self-contained, or that call other NOTA routines
which are loaded alongside. This keeps the tests honest - nothing is mocked
into passing.
"""
import os
import struct
import subprocess
import sys

from unicorn import Uc, UcError, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
from unicorn.arm64_const import (UC_ARM64_REG_SP, UC_ARM64_REG_X0,
                                 UC_ARM64_REG_X1, UC_ARM64_REG_X2,
                                 UC_ARM64_REG_X3, UC_ARM64_REG_X4,
                                 UC_ARM64_REG_LR, UC_ARM64_REG_PC,
                                 UC_ARM64_REG_X8)

CODE_BASE = 0x00100000
STACK_TOP = 0x002F0000
HEAP_BASE = 0x0F000000
DATA_BASE = 0x10000000
MEM_SIZE = 0x03000000

ZIG = os.environ.get("ZIG", "")


def toolchain_cc():
    if ZIG:
        return [ZIG, "cc", "-target", "aarch64-linux-android"]
    return ["zig", "cc", "-target", "aarch64-linux-android"]


class Harness:
    def __init__(self, extra_syms=None):
        self.uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        # Map region by region; address 0 stays unmapped so a `ret` with LR=0
        # faults immediately and marks the end of the call.
        self.uc.mem_map(0x1000, 0xEFF000)              # 0x1000..0xF00000
        self.uc.mem_map(0x0F000000, 0x01000000)        # heap 0x0F000000..0x10000000
        self.uc.mem_map(0x10000000, 0x01000000)        # data
        self.heap = HEAP_BASE
        self.data = DATA_BASE
        self.extra = extra_syms or {}
        self.uc.reg_write(UC_ARM64_REG_SP, STACK_TOP)
        self.uc.reg_write(UC_ARM64_REG_LR, 0)
        self.insn_count = 0
        # Count executed instructions so benchmarks measure real CPU work.
        self.uc.hook_add(UC_HOOK_CODE, self._hook_insn)

    def _hook_insn(self, uc, address, size, user):
        self.insn_count += 1

    def load(self, path):
        """Load a linked flat ELF (or bare blob) by its PT_LOAD segments."""
        from elftools.elf.elffile import ELFFile

        data = open(path, "rb").read()
        with open(path, "rb") as fh:
            elf = ELFFile(fh)
            segs = [s for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        if not segs:
            self.uc.mem_write(CODE_BASE, data)
            return CODE_BASE
        for seg in segs:
            off, vaddr = seg["p_offset"], seg["p_vaddr"]
            self.uc.mem_write(vaddr, data[off:off + seg["p_filesz"]])
            if seg["p_memsz"] > seg["p_filesz"]:
                self.uc.mem_write(vaddr + seg["p_filesz"],
                                  b"\x00" * (seg["p_memsz"] - seg["p_filesz"]))
        return segs[0]["p_vaddr"]

    def set_sym(self, name, addr):
        self.uc.reg_write(UC_ARM64_REG_PC, addr)

    def write_asm(self, code, base=CODE_BASE):
        self.uc.mem_write(base, code)

    def call(self, addr, args=(), instr_limit=200000):
        """Call addr with args, stop at `ret` (LR = 0). Returns x0."""
        self.uc.reg_write(UC_ARM64_REG_LR, 0)
        regs = [UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2,
                UC_ARM64_REG_X3, UC_ARM64_REG_X4]
        for i, a in enumerate(args):
            self.uc.reg_write(regs[i], a & 0xFFFFFFFFFFFFFFFF)
        self.uc.reg_write(UC_ARM64_REG_PC, addr)
        try:
            self.uc.emu_start(addr, 0, count=instr_limit)
        except UcError as exc:
            pc = self.uc.reg_read(UC_ARM64_REG_PC)
            if pc != 0:
                raise RuntimeError(f"fault at PC={pc:#x}: {exc}")
        return self.uc.reg_read(UC_ARM64_REG_X0)

    # -- memory helpers ---------------------------------------------------
    def alloc_str(self, s):
        b = s.encode() + b"\x00" if isinstance(s, str) else s + b"\x00"
        addr = self.data
        self.uc.mem_write(addr, b)
        self.data += (len(b) + 15) & ~15
        return addr

    def alloc(self, n):
        addr = self.heap
        self.heap += (n + 15) & ~15
        return addr

    def read(self, addr, n):
        return self.uc.mem_read(addr, n)

    def read_cstr(self, addr, limit=4096):
        out = bytearray()
        for i in range(limit):
            c = self.uc.mem_read(addr + i, 1)[0]
            if c == 0:
                break
            out.append(c)
        return bytes(out)


def assemble(sources, out_obj, include="src"):
    cc = toolchain_cc() + ["-c", "-fPIC", "-nostdlib", f"-I{include}", "-o", out_obj]
    cmd = cc + list(sources)
    subprocess.run(cmd, check=True)


def link_flat(objs, out_elf):
    cc = toolchain_cc() + ["-nostdlib", "-fuse-ld=lld", "-Wl,-Ttext=0x100000",
                           "-Wl,-e,_start", "-static", "-o", out_elf]
    subprocess.run(cc + list(objs), check=True)


def symbol_map(elf_path):
    """Return {symbol: address} for a linked flat binary."""
    from elftools.elf.elffile import ELFFile

    out = {}
    with open(elf_path, "rb") as fh:
        elf = ELFFile(fh)
        for sec in elf.iter_sections():
            if sec.name == ".symtab":
                for sym in sec.iter_symbols():
                    if sym.name and sym["st_value"]:
                        out[sym.name] = sym["st_value"]
    return out
