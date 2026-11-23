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

SOURCES = ["src/libc.S", "src/utf8.S", "src/sys.S"]          # flat-testable modules (no bionic calls)
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
