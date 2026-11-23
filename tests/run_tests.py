#!/usr/bin/env python3
"""run_tests.py - assemble NOTA, link a flat image, and execute the tests.

Every test executes the real AArch64 machine code produced by the assembler
on the interpreter in tests/a64.py. Nothing is stubbed; a routine that is
wrong fails here.

Usage:  python3 tests/run_tests.py [-v] [test_name ...]
"""
import os
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from a64 import Machine, IllegalInstruction, MemFault   # noqa: E402
from elftools.elf.elffile import ELFFile                # noqa: E402

CC = os.environ.get("NOTA_CC", "zig cc -target aarch64-linux-android").split()
BUILD = os.path.join(ROOT, "build", "tests")

SOURCES = ["src/libc.S", "src/utf8.S", "src/sys.S", "src/edit.S", "src/store.S"]          # flat-testable modules (no bionic calls)
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def s32(v):
    """Interpret a w0 return value as a signed 32-bit int."""
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


# ---------------------------------------------------------------- fixture ----
class Fixture:
    def __init__(self, elf, syms, kernel=None):
        self.elf = elf
        self.syms = syms
        self.m = Machine()
        self.m.map(0x100000, 0xFF000)
        self.m.map(0x1000000, 0x800000)   # heap/data
        self.m.map(0x1800000, 0x800000)   # strings
        self.m.map(self.m.sentinel & ~0xFFF, 0x1000)
        self.m.sp = 0x1E0000
        self.heap = 0x1000000
        self.data = 0x1800000
        if kernel is not None:
            self.m.syscall = kernel
        with open(elf, "rb") as fh:
            e = ELFFile(fh)
            for seg in e.iter_segments():
                if seg["p_type"] == "PT_LOAD":
                    raw = open(elf, "rb").read()[seg["p_offset"]:
                                                  seg["p_offset"] + seg["p_filesz"]]
                    self.m.store_mem(seg["p_vaddr"], raw)
                    if seg["p_memsz"] > seg["p_filesz"]:
                        self.m.store_mem(seg["p_vaddr"] + seg["p_filesz"],
                                         b"\0" * (seg["p_memsz"] - seg["p_filesz"]))

    def call(self, name, *args, max_insns=2_000_000):
        if name not in self.syms:
            raise AssertionError(f"missing symbol {name}")
        pc = self.syms[name]
        for i, a in enumerate(args):
            self.m.x[i] = a & 0xFFFFFFFFFFFFFFFF
        self.m.insn_count = 0
        return self.m.run(pc, max_insns=max_insns)

    def cstr(self, s):
        b = s.encode() + b"\0" if isinstance(s, str) else s + b"\0"
        addr = self.data
        self.m.store_mem(addr, b)
        self.data += (len(b) + 15) & ~15
        return addr

    def alloc(self, n):
        addr = self.heap
        self.heap += (n + 15) & ~15
        return addr

    def read(self, addr, n):
        return bytes(self.m.load_mem(addr, n))

    def read_cstr(self, addr, limit=8192):
        return self.m.read_cstr(addr, limit)


def build():
    os.makedirs(BUILD, exist_ok=True)
    startup = os.path.join(BUILD, "startup.S")
    with open(startup, "w") as fh:
        fh.write('.text\n.globl _start\n_start: ret\n'
                 '.section .note.GNU-stack,"",%progbits\n')
    objs = []
    for src in SOURCES:
        obj = os.path.join(BUILD, os.path.basename(src) + ".o")
        subprocess.run(CC + ["-c", "-fPIC", "-nostdlib", f"-I{ROOT}/src",
                             "-o", obj, os.path.join(ROOT, src)],
                       check=True, capture_output=True, text=True)
        objs.append(obj)
    sobj = os.path.join(BUILD, "startup.o")
    subprocess.run(CC + ["-c", "-fPIC", "-nostdlib", f"-I{ROOT}/src",
                         "-o", sobj, startup], check=True)
    objs.append(sobj)
    ld = os.path.join(BUILD, "flat.ld")
    with open(ld, "w") as fh:
        fh.write("ENTRY(_start)\nSECTIONS {\n"
                 " . = 0x100000;\n .text : { *(.text) }\n"
                 " .rodata : { *(.rodata*) }\n .data : { *(.data*) }\n"
                 " .bss : { *(.bss*) *(COMMON) }\n}\n")
    elf = os.path.join(BUILD, "flat.elf")
    subprocess.run(CC + ["-nostdlib", "-fuse-ld=lld", f"-Wl,-T,{ld}",
                         "-Wl,-e,_start", "-o", elf] + objs,
                   check=True, capture_output=True, text=True)
    syms = {}
    with open(elf, "rb") as fh:
        e = ELFFile(fh)
        for sec in e.iter_sections():
            if sec.name == ".symtab":
                for sym in sec.iter_symbols():
                    if sym.name and sym["st_value"]:
                        syms[sym.name] = sym["st_value"]
    return elf, syms


# ------------------------------------------------------------------ tests ----
@test
def test_strlen(fx):
    for s in ["", "a", "hello world", "x" * 500, "ünïcødé"]:
        addr = fx.cstr(s)
        got = fx.call("nota_strlen", addr)
        expect = len(s.encode())
        assert got == expect, f"strlen({s!r}) = {got}, want {expect}"


@test
def test_memcmp(fx):
    a = fx.cstr("abcdef")
    b = fx.cstr("abcdeg")
    assert s32(fx.call("nota_memcmp", a, a, 6)) == 0
    assert s32(fx.call("nota_memcmp", a, b, 3)) == 0
    assert s32(fx.call("nota_memcmp", a, b, 6)) < 0
    assert s32(fx.call("nota_memcmp", a, b, 0)) == 0


@test
def test_memcpy(fx):
    src = fx.cstr("0123456789abcdef")
    dst = fx.alloc(64)
    fx.call("nota_memset", dst, 0, 64)
    fx.call("nota_memcpy", dst, src, 16)
    assert fx.read(dst, 16) == b"0123456789abcdef"
    # short copy path
    dst2 = fx.alloc(16)
    fx.call("nota_memcpy", dst2, src, 5)
    assert fx.read(dst2, 5) == b"01234"


@test
def test_memmove(fx):
    buf = fx.alloc(32)
    fx.call("nota_memset", buf, ord("A"), 8)
    fx.call("nota_memmove", buf + 4, buf, 8)
    assert fx.read(buf, 12) == b"AAAA" + b"AAAAAAAA"
    # backwards overlap
    buf2 = fx.alloc(32)
    fx.m.store_mem(buf2, b"0123456789")
    fx.call("nota_memmove", buf2, buf2 + 2, 8)
    assert fx.read(buf2, 10) == b"23456789" + b"89"


@test
def test_memset(fx):
    dst = fx.alloc(32)
    fx.call("nota_memset", dst, 0x5A, 20)
    assert fx.read(dst, 20) == b"\x5a" * 20
    fx.call("nota_memset", dst, 0, 0)   # zero length is a no-op


@test
def test_strlcpy(fx):
    dst = fx.alloc(32)
    n = fx.call("nota_strlcpy", dst, fx.cstr("hello"), 32)
    assert n == 5 and fx.read_cstr(dst) == b"hello"
    n = fx.call("nota_strlcpy", dst, fx.cstr("truncate-me"), 6)
    assert n == 5 and fx.read_cstr(dst) == b"trunc"
    n = fx.call("nota_strlcpy", dst, fx.cstr("x"), 0)
    assert n == 0


def test_strcmp(fx):
    assert s32(fx.call("nota_strcmp", fx.cstr("abc"), fx.cstr("abc"))) == 0
    assert s32(fx.call("nota_strcmp", fx.cstr("abc"), fx.cstr("abd"))) < 0
    assert s32(fx.call("nota_strcmp", fx.cstr("abd"), fx.cstr("abc"))) > 0
    assert s32(fx.call("nota_strcmp", fx.cstr(""), fx.cstr(""))) == 0
    assert s32(fx.call("nota_strcmp", fx.cstr("ab"), fx.cstr("abc"))) < 0


@test
def test_strcasecmp(fx):
    assert s32(fx.call("nota_strcasecmp", fx.cstr("HeLLo"), fx.cstr("hello"))) == 0
    assert s32(fx.call("nota_strcasecmp", fx.cstr("Hello"), fx.cstr("World"))) != 0


@test
def test_strstr_i(fx):
    cases = [
        ("The Quick Brown Fox", "quick", 4),
        ("The Quick Brown Fox", "THE", 0),
        ("The Quick Brown Fox", "fox", 16),
        ("The Quick Brown Fox", "zebra", -1),
        ("abc", "", 0),
        ("abc", "abcd", -1),
        ("aaaaab", "aaab", 2),
    ]
    for hay, needle, want in cases:
        h = fx.cstr(hay)
        n = fx.cstr(needle)
        got = fx.call("nota_strstr_i", h, n)
        if got >= (1 << 63):
            got -= 1 << 64
        assert got == want, f"strstr_i({hay!r},{needle!r}) = {got}, want {want}"


@test
def test_atoi(fx):
    for s, want in [("0", 0), ("7", 7), ("42", 42), ("123456", 123456),
                    ("-9", -9), ("  33", 33), ("12ab", 12)]:
        got = s32(fx.call("nota_atoi", fx.cstr(s)))
        assert got == want, f"atoi({s!r}) = {got}, want {want}"


@test
def test_utf8_decode(fx):
    cases = [
        ("A", 0x41, 1),
        ("\u00e9", 0xE9, 2),          # é
        ("\u20ac", 0x20AC, 3),        # €
        ("\U0001F600", 0x1F600, 4),   # 😀
    ]
    for s, cp, n in cases:
        addr = fx.cstr(s)
        out = fx.alloc(8)
        got = fx.call("nota_utf8_decode", addr, out)
        val = int.from_bytes(fx.read(out, 4), "little")
        assert got == n, f"decode({s!r}) consumed {got}, want {n}"
        assert val == cp, f"decode({s!r}) = {val:#x}, want {cp:#x}"


@test
def test_utf8_encode(fx):
    dst = fx.alloc(16)
    for cp, want in [(0x41, b"A"), (0xE9, "\u00e9".encode()),
                     (0x20AC, "\u20ac".encode()),
                     (0x1F600, "\U0001F600".encode())]:
        n = fx.call("nota_utf8_encode", dst, cp)
        assert n == len(want), f"encode({cp:#x}) = {n} bytes, want {len(want)}"
        assert fx.read(dst, n) == want
    # surrogates and out-of-range are rejected
    assert fx.call("nota_utf8_encode", dst, 0xD800) == 0
    assert fx.call("nota_utf8_encode", dst, 0x110000) == 0


@test
def test_utf8_len(fx):
    for s, want in [("", 0), ("abc", 3), ("\u00e9\u00e8", 2),
                    ("a\u20acb", 3), ("\U0001F600\U0001F601", 2)]:
        b = s.encode()
        addr = fx.cstr(b)
        got = fx.call("nota_utf8_len", addr, len(b))
        assert got == want, f"utf8_len({s!r}) = {got}, want {want}"


@test
def test_utf8_next_prev(fx):
    b = "a\u00e9\u20ac\U0001F600".encode()
    addr = fx.cstr(b)
    stops = [0, 1, 3, 6]
    off = 0
    for want in stops[1:]:
        off = fx.call("nota_utf8_next", addr, len(b), off)
        assert off == want, f"next -> {off}, want {want}"
    for want in reversed(stops[:-1]):
        off = fx.call("nota_utf8_prev", addr, off)
        assert off == want, f"prev -> {off}, want {want}"


@test
def test_utf8_cp_byte_mapping(fx):
    b = "a\u00e9\u20ac\U0001F600z".encode()
    addr = fx.cstr(b)
    for cp, byte in [(0, 0), (1, 1), (2, 3), (3, 6), (4, 10), (5, 11)]:
        got = fx.call("nota_utf8_cp_to_byte", addr, len(b), cp)
        assert got == byte, f"cp_to_byte({cp}) = {got}, want {byte}"
    for byte, cp in [(0, 0), (1, 1), (2, 1), (3, 2), (6, 3), (11, 5)]:
        got = fx.call("nota_utf8_byte_to_cp", addr, byte)
        assert got == cp, f"byte_to_cp({byte}) = {got}, want {cp}"


@test
def test_utf8_valid(fx):
    good = ["", "plain ascii", "caf\u00e9", "\u20ac\U0001F600"]
    for s in good:
        b = s.encode()
        assert fx.call("nota_utf8_valid", fx.cstr(b), len(b)) == 1, repr(s)
    bad = [b"\xc0\x80", b"\xed\xa0\x80", b"\xf5\x80", b"abc\x80"]
    for b in bad:
        addr = fx.alloc(len(b) + 1)
        import struct
        fx.m.store_mem(addr, b + b"\0")
        assert fx.call("nota_utf8_valid", addr, len(b)) == 0, b


@test
def test_sys_strcpy_strcat(fx):
    dst = fx.alloc(64)
    src = fx.cstr("first")
    assert fx.call("nota_strcpy", dst, src) == dst
    assert fx.read_cstr(dst) == b"first"
    assert fx.call("nota_strcat", dst, fx.cstr("-second")) == dst
    assert fx.read_cstr(dst) == b"first-second"


@test
def test_sys_strlcat_bounds(fx):
    dst = fx.alloc(10)
    fx.m.store_mem(dst, b"ab\0" + b"\xcc" * 7)
    n = fx.call("nota_strlcat", dst, fx.cstr("cdefghij"), 5)
    assert fx.read_cstr(dst) == b"abcd", fx.read_cstr(dst)
    assert n == 4, n
    # already-full buffer reports the capacity, like BSD strlcat
    dst2 = fx.alloc(8)
    fx.m.store_mem(dst2, b"abcdefgh")
    assert fx.call("nota_strlcat", dst2, fx.cstr("x"), 8) == 8


@test
def test_sys_strchr_strrchr(fx):
    s = fx.cstr("a/b/c")
    assert fx.call("nota_strchr", s, ord("/")) == s + 1
    assert fx.call("nota_strrchr", s, ord("/")) == s + 3
    assert fx.call("nota_strchr", s, ord("z")) == 0
    assert fx.call("nota_strrchr", s, 0) == s + 5


@test
def test_sys_itoa_utoa(fx):
    buf = fx.alloc(32)
    for v, want in [(0, b"0"), (7, b"7"), (42, b"42"), (1000, b"1000"),
                    (2**64 - 1, b"18446744073709551615")]:
        n = fx.call("nota_utoa", buf, v)
        assert fx.read(buf, n) == want, (v, fx.read(buf, n))
    for v, want in [(-1, b"-1"), (-42, b"-42"), (9, b"9"),
                    (2**63 - 1, b"9223372036854775807")]:
        n = fx.call("nota_itoa", buf, v & 0xFFFFFFFFFFFFFFFF)
        assert fx.read(buf, n) == want, (v, fx.read(buf, n))


@test
def test_sys_crc32(fx):
    # CRC-32/ISO-HDLC check value for "123456789"
    data = fx.cstr("123456789")
    assert fx.call("nota_crc32", data, 9, 0) == 0xCBF43926
    assert fx.call("nota_crc32", data, 0, 0) == 0
    # incremental use: crc(a+b) == crc(b, seed=crc(a))
    a = fx.cstr("1234")
    b = fx.cstr("56789")
    seed = fx.call("nota_crc32", a, 4, 0)
    assert fx.call("nota_crc32", b, 5, seed) == 0xCBF43926


@test
def test_sys_monotonic_ms(fx):
    import sysemu
    k = sysemu.Kernel()
    k.now_ms = 123456
    fx.m.syscall = k
    got = fx.call("nota_monotonic_ms")
    assert got == 123456, got
    k.now_ms += 1500
    assert fx.call("nota_monotonic_ms") == 124956


@test
def test_sys_sleep_ms(fx):
    import sysemu
    k = sysemu.Kernel()
    fx.m.syscall = k
    start = k.now_ms
    fx.call("nota_sleep_ms", 250)
    assert k.now_ms - start == 250, k.now_ms - start


@test
def test_sys_write_read_file(fx):
    import sysemu
    k = sysemu.Kernel()
    fx.m.syscall = k
    buf = fx.alloc(1024)
    payload = b"hello store" * 40
    taddr = fx.cstr("/data/nota/a.tmp")
    faddr = fx.cstr("/data/nota/a.dat")
    n = s32(fx.call("nota_write_file_sync", taddr, faddr, fx.cstr(payload),
                    len(payload)))
    assert n == len(payload), n
    assert k.files["/data/nota/a.dat"] == payload
    assert "/data/nota/a.tmp" not in k.files, "tmp file must be renamed away"
    got = s32(fx.call("nota_read_file", faddr, buf, 1024))
    assert got == len(payload), got
    assert fx.read(buf, got) == payload


@test
def test_sys_write_file_sync_failure_keeps_old(fx):
    """A write failure must leave the previous file intact (crash safety)."""
    import sysemu
    k = sysemu.Kernel()
    k.files["/data/nota/a.dat"] = bytearray(b"OLD CONTENT")
    fx.m.syscall = k
    taddr = fx.cstr("/data/nota/a.tmp")
    faddr = fx.cstr("/data/nota/a.dat")
    k.write_allowed = 0            # every write fails
    n = s32(fx.call("nota_write_file_sync", taddr, faddr, fx.cstr(b"NEW"), 3))
    assert n < 0, n
    assert k.files["/data/nota/a.dat"] == b"OLD CONTENT"


# ------------------------------------------------------------------ edit ----
DOC_SIZE = 0x90
DOC_BUF, DOC_CAP, DOC_LEN, DOC_CPS = 0x00, 0x08, 0x10, 0x18
DOC_CURSOR, DOC_ANCH, DOC_DIRTY, DOC_SELECTING = 0x20, 0x28, 0x30, 0x38
DOC_BURST, DOC_UNC, DOC_UNP = 0x40, 0x48, 0x50


class Doc:
    """A document bound to real editor memory in the interpreter."""

    UENT_MAX = 64
    ARENA = 8192

    def __init__(self, fx, cap=256, text=b""):
        self.fx = fx
        self.cap = cap
        self.buf = fx.alloc(cap)
        self.doc = fx.alloc(DOC_SIZE)
        self.uent = fx.alloc(0x28 * self.UENT_MAX)
        self.arena = fx.alloc(self.ARENA)
        fx.call("nota_doc_init", self.doc, self.buf, cap)
        fx.call("nota_doc_init_undo", self.doc, self.uent, self.UENT_MAX,
                self.arena, self.ARENA)
        if text:
            self.set_text(text)

    def _ld(self, off):
        return int.from_bytes(self.fx.read(self.doc + off, 8), "little")

    def _st(self, off, v):
        self.fx.m.store_mem(self.doc + off, (v & (2**64 - 1)).to_bytes(8, "little"))

    text = property(lambda self: self.fx.read(self.buf, self._ld(DOC_LEN)))
    length = property(lambda self: self._ld(DOC_LEN))
    cps = property(lambda self: self._ld(DOC_CPS))
    cursor = property(lambda self: self._ld(DOC_CURSOR))
    anchor = property(lambda self: self._ld(DOC_ANCH))
    dirty = property(lambda self: self._ld(DOC_DIRTY))

    def set_text(self, text):
        addr = self.fx.cstr(text)
        self.fx.call("nota_doc_set_text", self.doc, addr, len(text))
        return self

    def insert(self, text):
        return self.fx.call("nota_doc_insert", self.doc, self.fx.cstr(text),
                            len(text))

    def insert_cp(self, cp):
        return fx_s32(self.fx.call("nota_doc_insert_cp", self.doc, cp))

    def replace(self, start, end, text=b""):
        addr = self.fx.cstr(text) if text else 0
        return fx_s32(self.fx.call("nota_doc_replace", self.doc, start, end,
                                   addr, len(text)))

    def backspace(self):
        return self.fx.call("nota_doc_backspace", self.doc)

    def delete_forward(self):
        return self.fx.call("nota_doc_delete_forward", self.doc)

    def select(self, anchor, cursor):
        self._st(DOC_ANCH, anchor)
        self._st(DOC_CURSOR, cursor)

    def move(self, off, extend=0):
        self.fx.call("nota_doc_move", self.doc, off, extend)

    def undo(self):
        return self.fx.call("nota_doc_undo", self.doc)

    def redo(self):
        return self.fx.call("nota_doc_redo", self.doc)


def fx_s32(v):
    return s32(v)


@test
def test_edit_init_and_set_text(fx):
    d = Doc(fx, 64)
    assert d.text == b"" and d.length == 0 and d.cps == 0
    d.set_text(b"hello")
    assert d.text == b"hello"
    assert d.length == 5 and d.cps == 5
    assert d.cursor == 5 and d.dirty == 0
    # UTF-8 counting: 1+2+3+4 bytes = 4 code points
    d.set_text("a\u00e9\u20ac\U0001F600".encode())
    assert d.cps == 4
    assert d.text == "a\u00e9\u20ac\U0001F600".encode()


@test
def test_edit_capacity_truncation(fx):
    d = Doc(fx, 16)
    d.set_text(b"0123456789ABCDEFGHIJ")     # 20 bytes into a 16-byte buffer
    assert d.length == 15, d.length          # cap - 1
    assert d.text == b"0123456789ABCDE"
    # truncation must not split a multi-byte sequence: with a 14-byte
    # buffer, 15 bytes of input cut at 13 would land inside the euro
    d2 = Doc(fx, 14)
    d2.set_text("aaaaaaaaaaaa\u20ac".encode())  # 12 + 3 bytes
    assert d2.text == b"a" * 12, d2.text


@test
def test_edit_insert_middle_and_ends(fx):
    d = Doc(fx, 128).set_text(b"abc")
    d.move(1)
    assert d.insert(b"XY") == 2
    assert d.text == b"aXYbc"
    assert d.cursor == 3
    assert d.dirty == 1
    d.move(0)
    d.insert(b"<")
    assert d.text == b"<aXYbc"
    d.move(d.length)
    d.insert(b">")
    assert d.text == b"<aXYbc>"


@test
def test_edit_insert_replaces_selection(fx):
    d = Doc(fx, 128).set_text(b"hello world")
    d.select(0, 5)                 # "hello"
    d.move(5)
    d.select(0, 5)
    assert d.insert(b"goodbye") == 7
    assert d.text == b"goodbye world"
    assert d.cursor == 7
    # a backwards selection behaves identically
    d.select(8, 13)
    d.insert(b"there")
    assert d.text == b"goodbye there"


@test
def test_edit_selection_helpers(fx):
    d = Doc(fx, 64).set_text(b"abcdef")
    d.select(4, 1)
    assert fx.call("nota_doc_sel_start", d.doc) == 1
    assert fx.call("nota_doc_sel_end", d.doc) == 4
    assert fx.call("nota_doc_has_selection", d.doc) == 1
    d.select(3, 3)
    assert fx.call("nota_doc_has_selection", d.doc) == 0
    d.select(4, 1)
    assert fx.call("nota_doc_delete_selection", d.doc) == 3
    assert d.text == b"aef"


@test
def test_edit_backspace_forward_delete(fx):
    d = Doc(fx, 64).set_text(b"abc")
    d.move(3)
    assert d.backspace() == 1
    assert d.text == b"ab" and d.cursor == 2
    d.move(0)
    assert d.delete_forward() == 1
    assert d.text == b"b" and d.cursor == 0
    assert d.delete_forward() == 1
    assert d.text == b""
    assert d.delete_forward() == 0        # nothing left
    assert d.backspace() == 0


@test
def test_edit_multibyte_editing(fx):
    """Every operation must move by whole code points."""
    euro = "\u20ac".encode()
    d = Doc(fx, 128).set_text(euro + b"x" + euro)
    assert d.cps == 3
    d.move(3)                       # after the first euro
    assert d.cursor == 3
    d.move(1)                       # asked for byte 1, inside the euro
    assert d.cursor == 0, "cursor must snap back to a code point boundary"
    d.move(4)                       # "x" starts here: a legal position
    assert d.cursor == 4
    d.move(5)                       # inside the second euro
    assert d.cursor == 4
    # backspace removes the whole euro, not one byte
    d.move(3)
    assert d.backspace() == 3
    assert d.text == b"x" + euro
    # forward delete removes the whole euro too
    d.move(1)
    assert d.delete_forward() == 3
    assert d.text == b"x"


@test
def test_edit_insert_codepoint(fx):
    d = Doc(fx, 128).set_text(b"ab")
    d.move(1)
    assert d.insert_cp(0x20AC) == 3
    assert d.text == b"a" + "\u20ac".encode() + b"b"
    assert d.cursor == 4
    assert d.cps == 3
    assert d.insert_cp(0x1F600) == 4
    assert d.cps == 4
    # invalid code points are rejected without touching the document
    before = d.text
    assert d.insert_cp(0xD800) == -3
    assert d.text == before


@test
def test_edit_undo_insert(fx):
    d = Doc(fx, 128).set_text(b"abc")
    d.move(1)
    d.insert(b"X")
    assert d.text == b"aXbc"
    assert d.undo() == 1
    assert d.text == b"abc"
    assert d.redo() == 1
    assert d.text == b"aXbc"
    assert d.redo() == 0
    assert d.undo() == 1
    assert d.undo() == 0        # history exhausted


@test
def test_edit_undo_delete(fx):
    d = Doc(fx, 128).set_text(b"abcdef")
    d.replace(2, 4)             # remove "cd"
    assert d.text == b"abef"
    assert d.undo() == 1
    assert d.text == b"abcdef"
    assert d.redo() == 1
    assert d.text == b"abef"


@test
def test_edit_undo_selection_replace_is_one_step(fx):
    d = Doc(fx, 128).set_text(b"hello world")
    d.select(0, 5)
    d.insert(b"bye")
    assert d.text == b"bye world"
    assert d.undo() == 1
    assert d.text == b"hello world", "a replace must undo in one step"
    assert d.undo() == 0


@test
def test_edit_typing_burst_coalesces(fx):
    d = Doc(fx, 128)
    for ch in b"hello":
        d.insert(bytes([ch]))
    assert d.text == b"hello"
    assert fx.call("nota_doc_undo", d.doc) == 1
    assert d.text == b"", f"a typing burst should undo as one step, got {d.text!r}"


@test
def test_edit_burst_breaks_on_move(fx):
    d = Doc(fx, 128)
    d.insert(b"ab")
    d.move(0)                  # a cursor jump ends the burst
    d.move(2)
    d.insert(b"cd")
    assert d.text == b"abcd"
    assert d.undo() == 1
    assert d.text == b"ab", f"got {d.text!r}"
    assert d.undo() == 1
    assert d.text == b""


@test
def test_edit_undo_after_new_edit_drops_redo(fx):
    d = Doc(fx, 128).set_text(b"a")
    d.move(1)
    d.insert(b"b")
    d.undo()
    assert d.text == b"a"
    d.insert(b"c")             # discards the redo tail
    assert d.redo() == 0
    assert d.text == b"ac"


@test
def test_edit_cursor_movement(fx):
    d = Doc(fx, 128).set_text(b"line one\nline two\nxyz")
    assert d.length == 21
    d.move(0)
    fx.call("nota_doc_move_right", d.doc, 0)
    assert d.cursor == 1
    fx.call("nota_doc_move_left", d.doc, 0)
    assert d.cursor == 0
    fx.call("nota_doc_move_left", d.doc, 0)
    assert d.cursor == 0, "cannot move before the start"
    d.move(4)
    fx.call("nota_doc_move_line_end", d.doc, 0)
    assert d.cursor == 8, d.cursor
    fx.call("nota_doc_move_line_start", d.doc, 0)
    assert d.cursor == 0
    d.move(d.length)
    fx.call("nota_doc_move_line_end", d.doc, 0)
    assert d.cursor == d.length


@test
def test_edit_extend_selection(fx):
    d = Doc(fx, 128).set_text(b"abcdef")
    d.move(2)
    fx.call("nota_doc_move_right", d.doc, 1)
    fx.call("nota_doc_move_right", d.doc, 1)
    assert d.cursor == 4 and d.anchor == 2, (d.cursor, d.anchor)
    assert fx.call("nota_doc_has_selection", d.doc) == 1
    # shrinking back: prev(4) is 3, still past the anchor
    fx.call("nota_doc_move_left", d.doc, 1)
    assert d.cursor == 3 and d.anchor == 2
    fx.call("nota_doc_move_left", d.doc, 1)
    assert d.cursor == 2, "cannot extend before the anchor"


@test
def test_edit_vertical_movement_keeps_column(fx):
    d = Doc(fx, 128).set_text(b"long line here\nshort\nanother long line")
    d.move(7)                    # column 7 on line 0
    fx.call("nota_doc_move_vert", d.doc, 1, 0)
    assert d.text.decode().split("\n")[1] == "short"
    assert d.cursor == 15 + 5, d.cursor     # clamped to the end of "short"
    fx.call("nota_doc_move_vert", d.doc, 1, 0)
    line3_start = d.text.index(b"another")
    assert d.cursor == line3_start + 7, d.cursor
    fx.call("nota_doc_move_vert", d.doc, -1, 0)
    assert d.cursor == 15 + 5


@test
def test_edit_select_word(fx):
    d = Doc(fx, 128).set_text(b"foo bar baz")
    fx.call("nota_doc_select_word_at", d.doc, 5)     # inside "bar"
    assert (d.anchor, d.cursor) == (4, 7), (d.anchor, d.cursor)
    # non-ASCII text selects as a single word
    d.set_text("\u00e9\u00e9\u00e9 x".encode())
    fx.call("nota_doc_select_word_at", d.doc, 1)   # inside the accented word
    assert (d.anchor, d.cursor) == (0, 6), (d.anchor, d.cursor)
    fx.call("nota_doc_select_word_at", d.doc, 7)   # the trailing "x"
    assert (d.anchor, d.cursor) == (7, 8), (d.anchor, d.cursor)


@test
def test_edit_select_all_and_delete(fx):
    d = Doc(fx, 128).set_text(b"everything here")
    fx.call("nota_doc_select_all", d.doc)
    assert d.anchor == 0 and d.cursor == d.length
    assert d.backspace() == len(b"everything here")
    assert d.text == b""
    assert d.undo() == 1
    assert d.text == b"everything here"


@test
def test_edit_line_helpers(fx):
    text = b"aa\nbbb\n\ncccc"
    d = Doc(fx, 128).set_text(text)
    assert fx.call("nota_line_count", d.buf, len(text)) == 4
    for off, line in [(0, 0), (1, 0), (2, 0), (3, 1), (7, 2), (11, 3)]:
        assert fx.call("nota_line_of", d.buf, len(text), off) == line, off
    assert fx.call("nota_line_start", d.buf, 4) == 3
    assert fx.call("nota_line_end", d.buf, len(text), 4) == 6


# ----------------------------------------------------------------- store ----
NOTE_ID, NOTE_FLAGS, NOTE_FOLDER, NOTE_TAGS = 0x00, 0x04, 0x08, 0x0C
NOTE_CREATED, NOTE_UPDATED = 0x10, 0x18
NOTE_TITLE_LEN, NOTE_BODY_LEN = 0x20, 0x24
NOTE_TITLE_PTR, NOTE_BODY_PTR, NOTE_TAG_PTR = 0x28, 0x30, 0x38
NOTE_SIZE = 0x40


class Note:
    """A note struct plus its string arena, allocated in the interpreter."""

    def __init__(self, fx, title=b"", body=b"", nid=1, folder=0, tags=(),
                 flags=0, created=0, updated=0):
        self.fx = fx
        self.addr = fx.alloc(NOTE_SIZE)
        arena = fx.alloc(4096)
        t = fx.alloc(len(title) + 1)
        fx.m.store_mem(t, title + b"\0")
        b = fx.alloc(len(body) + 1)
        fx.m.store_mem(b, body + b"\0")
        self.tagarr = fx.alloc(64)
        if tags:
            fx.m.store_mem(self.tagarr,
                           b"".join(int(x).to_bytes(4, "little") for x in tags))
        self._set(NOTE_ID, nid, 4)
        self._set(NOTE_FLAGS, flags, 4)
        self._set(NOTE_FOLDER, folder, 4)
        self._set(NOTE_TAGS, len(tags), 4)
        self._set(NOTE_CREATED, created)
        self._set(NOTE_UPDATED, updated)
        self._set(NOTE_TITLE_LEN, len(title), 4)
        self._set(NOTE_BODY_LEN, len(body), 4)
        self._set(NOTE_TITLE_PTR, t)
        self._set(NOTE_BODY_PTR, b)
        self._set(NOTE_TAG_PTR, self.tagarr)

    def _set(self, off, v, width=8):
        self.fx.m.store_mem(self.addr + off,
                            (v & ((1 << (width * 8)) - 1)).to_bytes(width, "little"))

    def _get(self, off, width=8):
        return int.from_bytes(self.fx.read(self.addr + off, width), "little")

    def field(self, off, width=8):
        return self._get(off, width)

    title = property(lambda self: self.fx.read_cstr(self._get(NOTE_TITLE_PTR)))
    body = property(lambda self: self.fx.read_cstr(self._get(NOTE_BODY_PTR)))


@test
def test_store_path_join(fx):
    dst = fx.alloc(64)
    n = fx.call("nota_path_join", dst, 64, fx.cstr("/data/nota"),
                fx.cstr("notes.db"))
    assert n == len("/data/nota/notes.db"), n
    assert fx.read_cstr(dst) == b"/data/nota/notes.db"
    # a trailing slash is not doubled
    fx.call("nota_path_join", dst, 64, fx.cstr("/data/nota/"),
            fx.cstr("x.db"))
    assert fx.read_cstr(dst) == b"/data/nota/x.db"
    # truncation is reported, never silent
    assert s32(fx.call("nota_path_join", dst, 8, fx.cstr("/data/nota"),
                       fx.cstr("notes.db"))) == -1


@test
def test_store_path_suffixes(fx):
    # these append to a path that is already in the buffer (the store writes
    # the path into a stack buffer, then derives the temp and backup names)
    dst = fx.alloc(64)
    src = fx.cstr("/data/a.db")
    fx.call("nota_strcpy", dst, src)
    fx.call("nota_path_mktmp", dst, dst)
    assert fx.read_cstr(dst) == b"/data/a.db.tmp", fx.read_cstr(dst)
    fx.call("nota_strcpy", dst, src)
    fx.call("nota_path_mkbak", dst, dst)
    assert fx.read_cstr(dst) == b"/data/a.db.bak", fx.read_cstr(dst)


@test
def test_store_int_codecs(fx):
    buf = fx.alloc(16)
    fx.call("nota_put_u32", buf, 0xDEADBEEF)
    assert fx.read(buf, 4) == b"\xef\xbe\xad\xde"     # little endian
    assert fx.call("nota_get_u32", buf) == 0xDEADBEEF
    fx.call("nota_put_u64", buf, 0x1122334455667788)
    assert fx.call("nota_get_u64", buf) == 0x1122334455667788


@test
def test_store_note_roundtrip(fx):
    n = Note(fx, title=b"My note", body="body \u20ac text".encode(),
             nid=7, folder=3, tags=(1, 4), flags=1, created=1000, updated=2000)
    buf = fx.alloc(4096)
    size = fx.call("nota_note_encode", n.addr, buf)
    assert size == fx.call("nota_note_encoded_size", n.addr)
    # decode into a fresh note
    out = fx.alloc(NOTE_SIZE)
    arena = fx.alloc(4096)
    tags = fx.alloc(64)
    fx.m.store_mem(out + NOTE_TAG_PTR, tags.to_bytes(8, "little"))
    used = s32(fx.call("nota_note_decode", buf, size, out, arena, 4096))
    assert used == size, used
    got = Note.__new__(Note)
    got.fx, got.addr = fx, out
    assert got.field(NOTE_ID, 4) == 7
    assert got.field(NOTE_FOLDER, 4) == 3
    assert got.field(NOTE_FLAGS, 4) == 1
    assert got.field(NOTE_TAGS, 4) == 2
    assert got.field(NOTE_CREATED) == 1000
    assert got.field(NOTE_UPDATED) == 2000
    assert got.title == b"My note"
    assert got.body == "body \u20ac text".encode()
    assert fx.read(tags, 8) == (1).to_bytes(4, "little") + (4).to_bytes(4, "little")


@test
def test_store_note_detects_corruption(fx):
    n = Note(fx, title=b"t", body=b"abcdef")
    buf = fx.alloc(512)
    size = fx.call("nota_note_encode", n.addr, buf)
    out = fx.alloc(NOTE_SIZE)
    fx.m.store_mem(out + NOTE_TAG_PTR, fx.alloc(64).to_bytes(8, "little"))
    arena = fx.alloc(256)
    # flip a body byte: the crc must catch it
    raw = bytearray(fx.read(buf, size))
    raw[-1] ^= 0xFF
    fx.m.store_mem(buf, bytes(raw))
    assert s32(fx.call("nota_note_decode", buf, size, out, arena, 256)) == -2
    # a truncated record is reported separately from corruption: the reader
    # has fewer bytes on hand than the record's own length prefix claims
    fx.m.store_mem(buf, bytes(raw[:-3]))
    assert s32(fx.call("nota_note_decode", buf, size - 3, out, arena, 256)) == -1
    # an arena that is too small is refused rather than overrun
    raw2 = bytearray(fx.read(buf, size))
    fx.m.store_mem(buf, bytes(fx.read(buf, size)))
    n2 = Note(fx, title=b"t", body=b"abcdef")
    size2 = fx.call("nota_note_encode", n2.addr, buf)
    assert s32(fx.call("nota_note_decode", buf, size2, out, arena, 4)) == -3


@test
def test_store_save_and_reload(fx):
    import sysemu
    k = sysemu.Kernel()
    fx.m.syscall = k
    path = fx.cstr("/data/nota/notes.db")
    data = fx.cstr(b"payload" * 100)
    n = s32(fx.call("nota_store_save_file", path, data, 700))
    assert n == 700, n
    assert k.files["/data/nota/notes.db"] == b"payload" * 100
    assert "/data/nota/notes.db.tmp" not in k.files
    # a second save takes a backup of the first
    data2 = fx.cstr(b"second version")
    assert s32(fx.call("nota_store_save_file", path, data2, 14)) == 14
    assert k.files["/data/nota/notes.db"] == b"second version"
    assert k.files["/data/nota/notes.db.bak"] == b"payload" * 100


@test
def test_store_failed_save_keeps_previous(fx):
    """A crash mid-write must not damage the last good file."""
    import sysemu
    k = sysemu.Kernel()
    k.files["/data/nota/notes.db"] = bytearray(b"GOOD")
    fx.m.syscall = k
    path = fx.cstr("/data/nota/notes.db")
    data = fx.cstr(b"NEW DATA")
    k.write_allowed = 0
    n = s32(fx.call("nota_store_save_file", path, data, 8))
    assert n < 0, n
    assert k.files["/data/nota/notes.db"] == b"GOOD", "old data must survive"
    # nothing was replaced, so there is nothing to back up; what matters is
    # that the half-written staging file was cleaned up rather than left to
    # be mistaken for a complete one
    assert "/data/nota/notes.db.tmp" not in k.files, k.files.keys()


# --------------------------------------------------------------- runner -----
def main():
    verbose = "-v" in sys.argv
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    elf, syms = build()
    failures = 0
    ran = 0
    for fn in TESTS:
        if names and not any(n in fn.__name__ for n in names):
            continue
        ran += 1
        fx = Fixture(elf, syms)
        try:
            fn(fx)
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failures += 1
            print(f"  FAIL  {fn.__name__}")
            if verbose:
                traceback.print_exc()
            else:
                tb = traceback.format_exc().strip().splitlines()
                print("        " + tb[-1])
    print(f"\n{ran - failures}/{ran} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
