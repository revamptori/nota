#!/usr/bin/env python3
"""test_ui.py - execute the drawing and layout code and inspect its output.

The renderer's job is to turn application state into a vertex buffer, and that
is exactly the kind of thing that can be tested for real: `nota_gl_quad` writes
plain bytes into `nota_gl_vbuf`, and everything above it (layout, wrapping,
glyph lookup, hit-testing, the two screen painters) is ordinary code.

So the GL and EGL entry points are stubbed here at *link* time - each becomes a
`ret` - and every quad the UI emits is a real, inspectable record. A test can
assert on the exact rectangle a note title occupies, that a tap lands on the
row that is drawn under it, that a long note wraps rather than overflows, and
that the vertex buffer never runs off its end.

Usage:  python3 tests/test_ui.py [-v] [filter]
"""
import os
import struct
import subprocess
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import sysemu                                    # noqa: E402
from a64 import Machine                          # noqa: E402
from elftools.elf.elffile import ELFFile         # noqa: E402

CC = os.environ.get("NOTA_CC", "zig cc -target aarch64-linux-android").split()
BUILD = os.path.join(ROOT, "build", "ui")

# Everything the UI touches, minus anything that needs bionic.
SOURCES = ["libc", "utf8", "sys", "edit", "store", "font", "tex", "text",
           "ui", "screen", "app"]

# main.S owns nota_as but also _start and the JNI entry points, so only the
# state block is stubbed. Layout matters: store.S reads AS_ACTIVITY (offset 0)
# to find the activity and therefore internalDataPath.
DATA_STUBS = """
    .bss
    .p2align 4
    .globl nota_as
    .hidden nota_as
nota_as:
    .space 0x60
    .text
    .section .note.GNU-stack,"",%progbits
"""

# GL/EGL entry points, stubbed to `ret`. The set is deliberately explicit: a
# new GL call in src/ that is not listed here fails to link, which is a much
# better failure than a stub that silently returns garbage.
GL_SYMBOLS = """
glViewport glUseProgram glUniform2fv glUniform1i glActiveTexture
glBindTexture glGenTextures glTexParameteri glTexImage2D glGenBuffers
glBindBuffer glBufferData glBufferSubData glDrawArrays glClearColor glClear
glEnable glBlendFunc glCreateShader glShaderSource glCompileShader
glCreateProgram glAttachShader glLinkProgram glGetProgramiv
glGetAttribLocation glGetUniformLocation glGetShaderiv glGetShaderInfoLog
glGetProgramInfoLog glEnableVertexAttribArray glVertexAttribPointer
glDeleteShader glDeleteProgram glPixelStorei glGetIntegerv glGetString
eglGetDisplay eglInitialize eglChooseConfig eglCreateWindowSurface
eglCreateContext eglMakeCurrent eglSwapBuffers eglSwapInterval eglTerminate
eglDestroyContext eglDestroySurface
ANativeWindow_getWidth ANativeWindow_getHeight
ANativeWindow_setBuffersGeometry ANativeWindow_acquire ANativeWindow_release
"""

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def s32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def build():
    os.makedirs(BUILD, exist_ok=True)
    stubs = os.path.join(BUILD, "stubs.S")
    with open(stubs, "w") as fh:
        fh.write('    .text\n')
        for s in GL_SYMBOLS.split():
            fh.write(f'    .globl {s}\n    .hidden {s}\n'
                     f'    .type {s}, %function\n{s}:\n    ret\n')
        fh.write('    .section .note.GNU-stack,"",%progbits\n')
    startup = os.path.join(BUILD, "startup.S")
    with open(startup, "w") as fh:
        fh.write('.text\n.globl _start\n_start: ret\n'
                 '.section .note.GNU-stack,"",%progbits\n')

    objs = []
    stub = os.path.join(BUILD, "data_stubs.S")
    with open(stub, "w") as fh:
        fh.write(DATA_STUBS)
    stubobj = os.path.join(BUILD, "data_stubs.o")
    subprocess.run(CC + ["-c", "-fPIC", "-nostdlib", "-o", stubobj, stub],
                   check=True, capture_output=True)
    objs.append(stubobj)
    for name in SOURCES:
        obj = os.path.join(BUILD, name + ".o")
        src = os.path.join(ROOT, "src", name + ".S")
        r = subprocess.run(CC + ["-c", "-fPIC", "-nostdlib", f"-I{ROOT}/src",
                                 "-o", obj, src],
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"{src}:\n{r.stdout}{r.stderr}")
        objs.append(obj)
    for extra in (stubs, startup):
        obj = extra.replace(".S", ".o")
        subprocess.run(CC + ["-c", "-fPIC", "-nostdlib", f"-I{ROOT}/src",
                             "-o", obj, extra], check=True)
        objs.append(obj)

    ld = os.path.join(BUILD, "flat.ld")
    with open(ld, "w") as fh:
        fh.write("ENTRY(_start)\nSECTIONS {\n . = 0x100000;\n"
                 " .text : { *(.text) }\n .rodata : { *(.rodata*) }\n"
                 " .data : { *(.data*) }\n .bss : { *(.bss*) *(COMMON) }\n}\n")
    elf = os.path.join(BUILD, "flat.elf")
    r = subprocess.run(CC + ["-nostdlib", "-fuse-ld=lld", f"-Wl,-T,{ld}",
                             "-Wl,-e,_start", "-o", elf] + objs,
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"link failed:\n{r.stdout}{r.stderr}")

    syms = {}
    with open(elf, "rb") as fh:
        e = ELFFile(fh)
        for sec in e.iter_sections():
            if sec.name == ".symtab":
                for sym in sec.iter_symbols():
                    if sym.name and sym["st_value"]:
                        syms[sym.name] = sym["st_value"]
    return elf, syms


class Fx:
    """A loaded image plus the handful of conveniences the tests need."""

    def __init__(self, elf, syms):
        self.elf, self.syms = elf, syms
        self.m = Machine()
        # Note creation timestamps the note, which goes through the clock
        # syscall; the kernel double answers it.
        self.kernel = sysemu.Kernel()
        self.m.syscall = self.kernel
        with open(elf, "rb") as fh:
            e = ELFFile(fh)
            # Size the code mapping from the image itself: the font atlas alone
            # is 66 KiB and the wrap table ~32 KiB, so a fixed 2 MiB guess
            # silently becomes too small as the app grows.
            end = max(seg["p_vaddr"] + seg["p_memsz"] for seg in e.iter_segments()
                      if seg["p_type"] == "PT_LOAD")
        size = (end - 0x100000 + 0xFFF) & ~0xFFF
        self.m.map(0x100000, size)
        self.m.map(0x4000000, 0x800000)  # heap-ish (ui state lands here)
        self.m.map(0x8000000, 0x400000)  # strings
        self.m.map(self.m.sentinel & ~0xFFF, 0x1000)
        self.m.sp = 0x2000000
        self.data = 0x8000000
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

    def call(self, name, *args, max_insns=40_000_000):
        if name not in self.syms:
            raise AssertionError(f"missing symbol {name}")
        for i, a in enumerate(args):
            self.m.x[i] = a & 0xFFFFFFFFFFFFFFFF
        self.m.insn_count = 0
        return self.m.run(self.syms[name], max_insns=max_insns)

    def alloc(self, n):
        """Bump-allocate scratch, sharing the cursor cstr() uses so the two
        cannot hand out overlapping addresses."""
        addr = self.data
        self.data += (n + 15) & ~15
        return addr

    def cstr(self, s):
        b = (s.encode() if isinstance(s, str) else s) + b"\0"
        addr = self.data
        self.m.store_mem(addr, b)
        self.data += (len(b) + 15) & ~15
        return addr

    def sym(self, name):
        return self.syms[name]

    def rd(self, addr, n):
        return bytes(self.m.load_mem(addr, n))

    def u64(self, addr):
        return struct.unpack("<Q", self.rd(addr, 8))[0]

    def set_u64(self, addr, v):
        self.m.store_mem(addr, struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF))

    def u32(self, addr):
        return struct.unpack("<I", self.rd(addr, 4))[0]

    def set_u32(self, addr, v):
        self.m.store_mem(addr, struct.pack("<I", v & 0xFFFFFFFF))

    # ---- quads ----------------------------------------------------------
    def quads(self):
        """Every emitted quad as (x0, y0, x1, y1, r, g, b, a), parsed out of
        the vertex buffer using the count the renderer recorded."""
        st = self.sym("nota_gl")
        # GL_NVERTS is 0x28 in the state block
        nv = struct.unpack("<I", self.rd(st + 0x28, 4))[0]
        base = self.sym("nota_gl_vbuf")
        out = []
        for i in range(nv // 6):
            v = []
            for k in range(6):
                # VSTRIDE is 32 bytes: x, y, u, v, r, g, b, a - eight floats.
                off = base + (i * 6 + k) * 32
                v.append(struct.unpack("<8f", self.rd(off, 32)))
            # corners 0, 2 are the extremes of the quad
            xs = [p[0] for p in v]
            ys = [p[1] for p in v]
            out.append((min(xs), min(ys), max(xs), max(ys),
                        v[0][4], v[0][5], v[0][6], v[0][7]))
        return out

    def vert_count(self):
        st = self.sym("nota_gl")
        return struct.unpack("<I", self.rd(st + 0x28, 4))[0]

    def rgba(self, q):
        """The quad's colour as 0xAARRGGBB, so tests read like the source.

        A quad tuple is (x0, y0, x1, y1, r, g, b, a), matching the eight
        floats nota_gl_quad writes per vertex.
        """
        r, g, b, a = (int(round(c * 255)) for c in q[4:8])
        return (a << 24) | (r << 16) | (g << 8) | b


def qaddr(fx, name):
    return fx.sym(name)


# --------------------------------------------------------------- geometry ---
@test
def test_ui_isqrt(fx):
    for v in [0, 1, 2, 3, 4, 8, 9, 15, 16, 99, 100, 1000, 10000, 65535]:
        got = fx.call("nota_isqrt", v)
        want = int(v ** 0.5)
        assert got == want, f"isqrt({v}) = {got}, want {want}"


@test
def test_ui_layout_geometry(fx):
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_ui")
    w = fx.u64(st + 0x08)
    h = fx.u64(st + 0x10)
    rows = fx.u64(st + 0x28)
    topy = fx.u64(st + 0x38)
    rowh = fx.u64(st + 0x20)
    assert (w, h) == (1080, 2160), (w, h)
    assert rowh == 96, rowh
    # rows that fit, rounded down, at least one
    assert rows == (2160 - topy) // 96, rows
    assert rows >= 1
    # the button sits fully inside the surface, inset on the right and bottom
    bx, by, bs = fx.u64(st + 0x48), fx.u64(st + 0x40), fx.u64(st + 0x50)
    assert bx + bs <= 1080 and by + bs <= 2160, (bx, by, bs)
    assert bx >= 1080 - bs - 32, "button should hug the right edge"


@test
def test_ui_layout_tiny_surface(fx):
    """A surface shorter than the header must still report one row, not zero
    (which would divide by zero downstream) and not a negative count."""
    fx.call("nota_ui_layout", 320, 40)
    st = fx.sym("nota_ui")
    assert fx.u64(st + 0x28) == 1


@test
def test_ui_row_hit_testing(fx):
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_ui")
    topy = fx.u64(st + 0x38)
    # a tap above the first row is not a row
    assert s32(fx.call("nota_ui_row_at", 10)) == -1
    # a tap just inside the first row is row 0, and at its centre too
    assert s32(fx.call("nota_ui_row_at", topy + 1)) == 0
    assert s32(fx.call("nota_ui_row_at", topy + 48)) == 0
    # row boundaries are half-open: row 1 starts exactly one row down
    assert s32(fx.call("nota_ui_row_at", topy + 96)) == 1
    assert s32(fx.call("nota_ui_row_at", topy + 95)) == 0


@test
def test_ui_row_scroll_agrees_with_hittest(fx):
    """Scrolling shifts what is drawn and what is tappable by the same
    amount. This is the pair that silently drifts in a real app."""
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_ui")
    topy = fx.u64(st + 0x38)
    for scroll in (0, 1, 3, 17):
        fx.set_u64(st + 0x18, scroll)
        for row in (0, 1, 4):
            y = s32(fx.call("nota_ui_row_y", row)) + 20
            if y < topy:
                continue
            hit = s32(fx.call("nota_ui_row_at", y))
            if hit >= 0:
                assert hit == row, f"scroll={scroll} row={row}: drawn at {y}, hit {hit}"


@test
def test_ui_button_hit_testing(fx):
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_ui")
    bx, by, bs = fx.u64(st + 0x48), fx.u64(st + 0x40), fx.u64(st + 0x50)
    centre = (bx + bs // 2, by + bs // 2)
    assert s32(fx.call("nota_ui_hit_button", *centre)) == 1
    # just inside each edge
    assert s32(fx.call("nota_ui_hit_button", bx + 1, by + 1)) == 1
    assert s32(fx.call("nota_ui_hit_button", bx + bs - 1, by + bs - 1)) == 1
    # just outside: half-open bounds, so the far edges are not inside
    assert s32(fx.call("nota_ui_hit_button", bx - 1, by + 1)) == 0
    assert s32(fx.call("nota_ui_hit_button", bx + bs, by + bs // 2)) == 0
    assert s32(fx.call("nota_ui_hit_button", 0, 0)) == 0


# ------------------------------------------------------------------- text ---
@test
def test_glyph_lookup_covers_the_declared_ranges(fx):
    # ASCII maps to a dense run from index 0
    assert fx.call("nota_glyph", ord(" ")) == 0
    assert fx.call("nota_glyph", ord("A")) == ord("A") - 0x20
    assert fx.call("nota_glyph", ord("~")) == 0x7E - 0x20
    # Latin-1 follows at 95
    assert fx.call("nota_glyph", 0xA0) == 95
    assert fx.call("nota_glyph", 0xE9) == 95 + 0x49
    # strays are found by the table
    # Strays occupy 191..199 in ascending code-point order, which is what lets
    # nota_glyph stop the scan early; the euro sorts last, not first.
    assert s32(fx.call("nota_glyph", 0x2013)) == 191, "en dash"
    assert s32(fx.call("nota_glyph", 0x20AC)) == 199, "euro"
    # anything unmapped is -1, not 0 and not a crash
    assert s32(fx.call("nota_glyph", 0x4E00)) == -1, "CJK is not in the font"
    assert s32(fx.call("nota_glyph", 0x1F600)) == -1, "emoji are not in the font"


@test
def test_glyph_advances_are_sane(fx):
    """Every glyph in the atlas must have a positive advance no wider than its
    cell, or text will overlap or gap."""
    for cp in list(range(0x20, 0x7F)) + list(range(0xA0, 0x100)):
        idx = fx.call("nota_glyph", cp)
        adv = fx.call("nota_glyph_advance", idx)
        assert 2 <= adv <= 16, f"U+{cp:04X} idx {idx} advance {adv}"
    # space must be the narrowest common glyph
    assert fx.call("nota_glyph_advance", fx.call("nota_glyph", ord(" "))) <= 8


@test
def test_text_width_matches_sum_of_advances(fx):
    s = fx.cstr("Hamburgefonstiv")
    got = fx.call("nota_text_width", s, 17)
    want = sum(fx.call("nota_glyph_advance", fx.call("nota_glyph", ord(c)))
               for c in "Hamburgefonstiv")
    assert got == want, (got, want)


@test
def test_text_width_handles_utf8_and_unknown_glyphs(fx):
    """A multi-byte string measures by code point, not byte, and a code point
    with no glyph contributes nothing rather than corrupting the total."""
    ascii_w = fx.call("nota_text_width", fx.cstr("ee"), 2)
    utf8_w = fx.call("nota_text_width", fx.cstr("\u00e9\u00e9"), 4)
    assert ascii_w == utf8_w, (ascii_w, utf8_w)
    # unmapped code point: still advances nothing, still terminates
    w = fx.call("nota_text_width", fx.cstr("\u4e00"), 3)
    assert w == 0, w


@test
def test_text_width_stops_at_nul(fx):
    s = fx.cstr("abc")
    # passing a length longer than the string must not run past the NUL
    assert fx.call("nota_text_width", s, 100) == fx.call("nota_text_width", s, 3)


@test
def test_caret_x_matches_pen_position(fx):
    """The tap-to-caret mapping and the drawn caret must agree: clicking at
    the pen position of offset N must yield offset N."""
    s = fx.cstr("hello world")
    xs = []
    for i in range(11):
        xs.append(fx.call("nota_text_width", s, i))
    for i, x in enumerate(xs):
        got = s32(fx.call("nota_edit_offset_at_x", s, 11, x))
        assert got == i, f"pen x for offset {i} is {x}, mapped back to {got}"
    # tapping past the end pins to the end
    assert s32(fx.call("nota_edit_offset_at_x", s, 11, 10000)) == 11
    # tapping left of the start pins to the start
    assert s32(fx.call("nota_edit_offset_at_x", s, 11, -50)) == 0


# ------------------------------------------------------------------- wrap ---
@test
def test_wrap_breaks_at_spaces(fx):
    text = "the quick brown fox jumps over the lazy dog"
    s = fx.cstr(text)
    maxw = fx.call("nota_text_width", fx.cstr("the quick brown"), 15)
    n = fx.call("nota_ui_wrap", s, len(text), maxw)
    assert n >= 3, n
    wr = fx.sym("nota_wrap")
    # nota_ui_wrap stores the *address* of each line start, so the editor does
    # not have to re-add a base it might get wrong; convert to offsets here.
    starts = [fx.u64(wr + i * 8) - s for i in range(n)]
    assert starts[0] == 0
    # every line start is at or after the previous one, and no line overruns
    for i in range(n):
        end = starts[i + 1] if i + 1 < n else len(text)
        line = text[starts[i]:end].rstrip("\n")
        assert not line.startswith(" "), f"line {i} starts with a space: {line!r}"
        w = fx.call("nota_text_width", fx.cstr(line), len(line))
        assert w <= maxw, f"line {i} ({line!r}) is {w}px, limit {maxw}"


@test
def test_wrap_hard_breaks_an_unbreakable_word(fx):
    """A single word wider than the line must break, not overflow and not
    loop forever."""
    word = "x" * 200
    s = fx.cstr(word)
    maxw = 100
    n = fx.call("nota_ui_wrap", s, len(word), maxw)
    assert n > 1, n
    wr = fx.sym("nota_wrap")
    starts = [fx.u64(wr + i * 8) for i in range(n)]
    assert starts == sorted(starts), "line starts must advance"
    assert len(set(starts)) == n, f"duplicate line starts: {starts}"


@test
def test_wrap_respects_explicit_newlines(fx):
    text = "one\ntwo\nthree"
    s = fx.cstr(text)
    n = fx.call("nota_ui_wrap", s, len(text), 100000)  # never needs to wrap
    assert n == 3, n
    wr = fx.sym("nota_wrap")
    starts = [fx.u64(wr + i * 8) - s for i in range(n)]
    assert starts == [0, 4, 8], starts


@test
def test_wrap_of_empty_text_is_one_line(fx):
    s = fx.cstr("")
    assert fx.call("nota_ui_wrap", s, 0, 500) == 1


# ----------------------------------------------------------- vertex buffer ---
@test
def test_quad_emits_six_vertices(fx):
    st = fx.sym("nota_gl")
    fx.set_u64(st + 0x00, 1080)
    fx.set_u64(st + 0x08, 2160)
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_gl_color0", 0xFF112233)
    fx.call("nota_gl_quad", 10, 20, 110, 220, 0, 0, 16, 20)
    assert fx.vert_count() == 6
    q = fx.quads()[0]
    assert q[:4] == (10, 20, 110, 220), q[:4]
    assert fx.rgba(q) == 0xFF112233, hex(fx.rgba(q))


@test
def test_quad_colour_is_applied_from_state(fx):
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_gl_color0", 0x80FF8000)
    fx.call("nota_gl_quad", 0, 0, 1, 1, 0, 0, 1, 1)
    q = fx.quads()[0]
    a, r, g, b = fx.rgba(q) >> 24, (fx.rgba(q) >> 16) & 0xFF, \
        (fx.rgba(q) >> 8) & 0xFF, fx.rgba(q) & 0xFF
    assert (r, g, b) == (0xFF, 0x80, 0x00), (r, g, b)
    assert abs(a - 0x80) <= 1, a


@test
def test_quad_buffer_never_overflows(fx):
    """Filling past the buffer must stop cleanly. A corrupt frame is worse
    than a missing quad, and writing past the array would be worse still."""
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    base = fx.sym("nota_gl_vbuf")
    guard_off = base + 4096 * 32
    fx.m.store_mem(guard_off, b"\xAA" * 64)
    for i in range(1000):
        fx.call("nota_gl_quad", i, i, i + 1, i + 1, 0, 0, 1, 1)
    nv = fx.vert_count()
    assert nv <= 4096, nv
    assert fx.rd(guard_off, 64) == b"\xAA" * 64, "wrote past the vertex buffer"


@test
def test_solid_uses_one_texel(fx):
    """A solid rect must sample a single point, not interpolate across the
    atlas - otherwise a panel would smear the glyphs next to it."""
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_gl_color0", 0xFF00FF00)
    fx.call("nota_ui_solid", 0, 0, 100, 100)
    base = fx.sym("nota_gl_vbuf")
    uvs = set()
    for k in range(6):
        v = struct.unpack("<8f", fx.rd(base + k * 32, 32))
        uvs.add((round(v[2], 6), round(v[3], 6)))
    assert len(uvs) == 1, f"solid quad interpolated UVs: {uvs}"


@test
def test_round_rect_stays_within_bounds(fx):
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_gl_color0", 0xFF202020)
    fx.call("nota_ui_round_rect", 100, 200, 400, 300, 12, 0xFF202020)
    qs = fx.quads()
    assert qs, "round rect emitted nothing"
    for q in qs:
        assert q[0] >= 100 - 1 and q[1] >= 200 - 1, q
        assert q[2] <= 500 + 1 and q[3] <= 500 + 1, q


@test
def test_round_rect_degenerate_radius_is_a_rect(fx):
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_ui_round_rect", 10, 10, 50, 50, 0, 0xFF202020)
    assert len(fx.quads()) == 1, "radius 0 should be a single quad"


# ---------------------------------------------------------------- screens ---
def seed_notes(fx, texts):
    """Create notes with the given bodies through the real model code."""
    fx.call("nota_notes_init")
    for t in texts:
        n = fx.call("nota_note_new")
        assert n, "note_new returned NULL"
        # give it a body by committing an editor buffer
        buf = fx.sym("nota_editbuf")
        body = t.encode() if isinstance(t, str) else t
        fx.m.store_mem(buf, body + b"\0")
        doc = fx.sym("nota_doc")
        fx.set_u64(doc + 0x00, buf)
        fx.set_u64(doc + 0x08, 65536)
        fx.set_u64(doc + 0x10, len(body))
        # ED_NOTE is field 0 of nota_edit
        ed = fx.sym("nota_edit")
        fx.set_u64(ed + 0x00, n)
        fx.call("nota_note_commit")


@test
def test_list_screen_draws_a_row_per_note(fx):
    seed_notes(fx, [b"First note\nbody one", b"Second\nbody two"])
    fx.call("nota_ui_set_screen", 0)
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_screen_list")
    qs = fx.quads()
    assert qs, "the list drew nothing"

    # A card is drawn as a middle band plus two inset caps, so no single quad
    # is the card. Group the wide quads by row instead and rebuild each box.
    rowh = fx.u64(fx.sym("nota_ui") + 0x20)
    panels = [q for q in qs if (q[2] - q[0]) > 1000 and q[1] >= 72]
    # A card's parts are its top cap, middle band and bottom cap; the band's
    # top edge is the only one that lands strictly inside the row, so bucket
    # on the quad's centre instead of its top.
    rows = {}
    for q in panels:
        cy = (q[1] + q[3]) / 2
        rows.setdefault(int((cy - 72) // rowh), []).append(q)
    assert len(rows) == 2, f"expected 2 note cards, drew {len(rows)}"
    boxes = []
    for parts in rows.values():
        boxes.append((min(p[0] for p in parts), min(p[1] for p in parts),
                      max(p[2] for p in parts), max(p[3] for p in parts)))
    boxes.sort(key=lambda b: b[1])
    for b in boxes:
        assert abs((b[3] - b[1]) - (rowh - 8)) <= 1, f"card height {b[3] - b[1]}"
    assert abs((boxes[1][1] - boxes[0][1]) - rowh) <= 1, \
        f"rows {boxes[0][1]} and {boxes[1][1]} are not {rowh} apart"


@test
def test_list_row_geometry_matches_hit_testing(fx):
    """The card drawn for note N must be the one a tap on N opens."""
    seed_notes(fx, [b"one\nx", b"two\nx", b"three\nx"])
    fx.call("nota_ui_set_screen", 0)
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_screen_list")

    rowh = fx.u64(fx.sym("nota_ui") + 0x20)
    cards = [q for q in fx.quads()
             if abs((q[3] - q[1]) - (rowh - 8)) <= 1 and (q[2] - q[0]) > 500]
    cards.sort(key=lambda q: q[1])
    for i, c in enumerate(cards):
        y = int((c[1] + c[3]) // 2)
        hit = s32(fx.call("nota_ui_row_at", y))
        assert hit == i, f"card {i} at y={y} hit-tests as row {hit}"


@test
def test_empty_list_shows_an_empty_state(fx):
    fx.call("nota_notes_init")
    fx.call("nota_ui_set_screen", 0)
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_screen_list")
    assert fx.quads(), "an empty list should still draw its header"


@test
def test_list_virtualises_off_screen_rows(fx):
    """A thousand notes must not emit a thousand cards: only the rows that
    intersect the viewport are drawn."""
    fx.call("nota_notes_init")
    for i in range(200):
        fx.call("nota_note_new")
    fx.call("nota_ui_set_screen", 0)
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_screen_list")
    ui = fx.sym("nota_ui")
    rows = fx.u64(ui + 0x28)
    rowh = fx.u64(ui + 0x20)
    # One card per visible row; a card is three quads (two caps and a band),
    # so count the rows the wide quads fall into, not the quads.
    panels = [q for q in fx.quads() if (q[2] - q[0]) > 1000 and q[1] >= 72]
    buckets = {int((((q[1] + q[3]) / 2) - 72) // rowh) for q in panels}
    assert len(buckets) == rows, f"drew {len(buckets)} cards for {rows} visible rows"
    assert len(buckets) < 200, "the list drew every note, not just the visible ones"


@test
def test_list_draws_every_card_inside_the_surface(fx):
    seed_notes(fx, [("n%d\nbody" % i).encode() for i in range(5)])
    fx.call("nota_ui_set_screen", 0)
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_screen_list")
    for q in fx.quads():
        assert -1 <= q[0] and q[2] <= 1081, f"quad escapes width: {q}"
        assert -1 <= q[1], f"quad escapes top: {q}"


@test
def test_editor_screen_draws_text_and_caret(fx):
    seed_notes(fx, [b"Title line\nsecond line of the note"])
    fx.call("nota_note_open", 0)
    fx.call("nota_ui_set_screen", 1)
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_screen_edit")
    qs = fx.quads()
    assert qs, "editor drew nothing"

    # the caret is a narrow, tall quad in the cursor colour
    carets = [q for q in qs
              if abs(q[2] - q[0] - 2) <= 1 and (q[3] - q[1]) >= 20
              and fx.rgba(q) == 0xFFF2C94C]
    assert len(carets) == 1, f"expected exactly one caret, found {len(carets)}"


@test
def test_editor_text_wraps_within_the_margin(fx):
    long_note = ("word " * 80).encode()
    seed_notes(fx, [b"T\n" + long_note])
    fx.call("nota_note_open", 0)
    fx.call("nota_ui_set_screen", 1)
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_screen_edit")
    ui = fx.sym("nota_ui")
    wrapx = fx.u64(ui + 0x60)
    # text quads (those whose UVs are not the solid texel) must respect the margin
    for q in fx.quads():
        if (q[2] - q[0]) < 900:
            assert q[2] <= wrapx + 18, f"glyph escapes the wrap margin: {q}"


@test
def test_drawing_is_deterministic(fx):
    """Same state, same frame. Catches uninitialised state read during
    painting, which would otherwise show up as a flickering artefact."""
    seed_notes(fx, [b"alpha\nbeta", b"gamma\ndelta"])
    fx.call("nota_ui_set_screen", 0)
    fx.call("nota_ui_layout", 1080, 2160)
    st = fx.sym("nota_gl")
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_screen_list")
    first = fx.quads()
    fx.set_u32(st + 0x28, 0)
    fx.call("nota_screen_list")
    second = fx.quads()
    assert len(first) == len(second)
    for a, b in zip(first, second):
        assert a == b, f"non-deterministic quad: {a} vs {b}"


# ------------------------------------------------------------------ runner --
# ----------------------------------------------------------------- persistence ---
@test
def test_encoded_size_matches_encode_for_every_note_shape(fx):
    """encoded_size must equal what encode actually writes.

    These two routines walk the same fields separately, so they can drift.
    When encoded_size under-counts, the record length prefix is short and the
    reader runs off the end of the record; when it over-counts, the blob is
    written short and the final record is truncated. Either way the failure
    surfaces as a corrupt-file error on the next launch, far from the cause,
    so the invariant is asserted directly for each shape of note.
    """
    seed_notes(fx, [b"title only", b"multi\nline\nbody text", b"a" * 200])

    # a note with an explicit tag, which sums a different set of fields again
    n = fx.call("nota_note_new")
    fx.set_u64(n + 0x38, fx.cstr("work"))   # NOTE_TAG_PTR
    fx.set_u32(n + 0x0C, 4)                 # NOTE_TAGS: the count

    buf = fx.sym("nota_blob")
    for i in range(fx.call("nota_note_count")):
        note = fx.call("nota_note_at", i)
        want = fx.call("nota_note_encoded_size", note)
        got = fx.call("nota_note_encode", note, buf)
        assert want == got, (
            f"note {i}: encoded_size says {want} bytes, encode wrote {got}")


@test
def test_every_glyph_advances_the_pen(fx):
    """Consecutive glyphs must advance the pen.

    The glyph index has to survive nota_gl_quad, which reuses x10 for the
    vertex count. When it did not, the advance was looked up for glyph 0 -
    advance 0 - so every character after the first was drawn on top of it and
    a row of text rendered as a single letter. Any text long enough to matter
    is affected, so assert on a real row rather than one character.
    """
    seed_notes(fx, [b"Thirty-four characters of text"])
    fx.call("nota_ui_layout", 1080, 2160)
    fx.call("nota_gl_frame", 1080, 2160)
    fx.call("nota_ui_set_screen", 0)
    fx.call("nota_screen_list")

    xs = sorted({round(q[0]) for q in fx.quads()
                 if q[2] - q[0] <= 17 and q[1] < 130})
    assert len(xs) > 10, f"only {len(xs)} distinct x positions: {xs[:6]}"
    assert xs == sorted(set(xs))
    # strictly increasing, and no two glyphs share a cell
    assert all(b > a for a, b in zip(xs, xs[1:])), xs


@test
def test_list_preview_skips_the_title_line(fx):
    """The body is the whole document, so the preview must not repeat line 1."""
    body = b"Title line\npreview line"
    seed_notes(fx, [body])
    fx.call("nota_ui_layout", 1080, 2160)
    fx.call("nota_gl_frame", 1080, 2160)
    fx.call("nota_ui_set_screen", 0)
    fx.call("nota_screen_list")

    # A glyph quad is exactly one 16x20 cell. Anything smaller or taller is a
    # rounded-corner fragment from the card behind it, which would otherwise
    # be mistaken for a row of two-letter text near the edges.
    rows = {}
    for q in fx.quads():
        if round(q[2] - q[0]) == 16 and round(q[3] - q[1]) == 20:
            rows.setdefault(round(q[1]), []).append(q[0])
    ys = sorted(rows)
    assert len(ys) >= 2, ys
    title_row, preview_row = rows[ys[0]], rows[ys[1]]
    assert min(title_row) == min(preview_row), "both rows start at the margin"

    # "Title line" and "preview line" share no leading letters, so the two rows
    # cannot start with the same glyph. Compare the atlas cells: if the preview
    # were repeating the title, the first glyph of each row would be identical.
    def first_cell(y):
        st = fx.sym("nota_gl")
        nv = fx.u32(st + 0x28)
        base = fx.sym("nota_gl_vbuf")
        x = min(rows[y])
        for i in range(nv // 6):
            v = struct.unpack("<8f", fx.rd(base + i * 6 * 32, 32))
            if round(v[0]) == round(x) and round(v[1]) == y:
                return (round(v[2] * 208) // 16, round(v[3] * 320) // 20)
        return None

    assert first_cell(ys[0]) != first_cell(ys[1]), (
        "preview repeats the title's first glyph")


@test
def test_store_path_joins_the_activitys_data_dir(fx):
    """nota_store_path must build <internalDataPath>/notes.db."""
    act = fx.alloc(0x60)
    fx.set_u64(act + 0x20, fx.cstr("/data/user/0/com.revamptori.nota/files"))
    fx.set_u64(fx.sym("nota_as") + 0x00, act)

    dst = fx.alloc(256)
    n = fx.call("nota_store_path", dst, 256)
    assert n == len("/data/user/0/com.revamptori.nota/files/notes.db"), n
    assert fx.rd(dst, n) == b"/data/user/0/com.revamptori.nota/files/notes.db"


@test
def test_store_path_does_not_double_the_separator(fx):
    """internalDataPath is documented without a trailing slash, but be safe."""
    act = fx.alloc(0x60)
    fx.set_u64(act + 0x20, fx.cstr("/data/files/"))
    fx.set_u64(fx.sym("nota_as") + 0x00, act)

    dst = fx.alloc(256)
    fx.call("nota_store_path", dst, 256)
    assert fx.rd(dst, 21) == b"/data/files/notes.db\0"


@test
def test_save_load_round_trip_preserves_tags(fx):
    """Tag ids survive the round trip as their own arena-backed array.

    A tag is an id into the tag table, not a string, so what has to survive is
    the u32 array itself. The record carries those ids, and load has to rebuild
    the array somewhere it owns: the pointer on the saved note referred to a
    buffer that no longer exists in the new process.
    """
    seed_notes(fx, [b"Tagged note\nwith a body"])
    n = fx.call("nota_note_at", 0)
    ids = fx.sym("nota_blob") + 0x100
    fx.m.store_mem(ids, struct.pack("<4I", 7, 11, 42, 300))
    fx.set_u64(n + 0x38, ids)      # NOTE_TAG_PTR
    fx.set_u32(n + 0x0C, 4)        # NOTE_TAGS: the count

    path = fx.cstr("/data/nota/tags.db")
    assert fx.call("nota_store_save", path) == 0
    fx.call("nota_notes_init")
    assert fx.call("nota_store_load", path) == 1

    r = fx.call("nota_note_at", 0)
    assert fx.u32(r + 0x0C) == 4
    got = struct.unpack("<4I", fx.rd(fx.u64(r + 0x38), 16))
    assert got == (7, 11, 42, 300), f"tag ids came back as {got}"


@test
def test_save_load_round_trip(fx):
    """Everything the app writes must read back identically."""
    seed_notes(fx, [b"First note\nfirst body", b"Second\n\nsecond body"])
    path = fx.cstr("/data/nota/notes.db")
    assert fx.call("nota_store_save", path) == 0
    rail = fx.kernel.files["/data/nota/notes.db"]
    assert len(rail) > 12, "nothing was written"
    assert bytes(rail[:4]) == b"NOTA", rail[:4]

    # wipe the collection, then reload it from the file
    fx.call("nota_notes_init")
    assert fx.u64(fx.sym("nota_store") + 8) == 0
    n = fx.call("nota_store_load", path)
    assert n == 2, f"loaded {n} notes"
    assert fx.u64(fx.sym("nota_store") + 8) == 2

    note = fx.call("nota_note_at", 0)
    blen = struct.unpack("<I", fx.rd(note + 0x24, 4))[0]
    body = fx.rd(fx.u64(note + 0x30), blen)
    assert body == b"First note\nfirst body", body


@test
def test_load_of_a_missing_file_is_empty_not_an_error(fx):
    """A first run has nothing to load and must come up empty."""
    n = fx.call("nota_store_load", fx.cstr("/data/nota/absent.db"))
    assert n == 0, n
    assert fx.u64(fx.sym("nota_store") + 8) == 0


@test
def test_load_rejects_a_corrupt_file(fx):
    """A bad record must stop the load, not half-apply it."""
    fx.kernel.files["/data/nota/bad.db"] = bytearray(b"NOTA" + b"\x01\x00\x00\x00"
                                                     + b"\x05\x00\x00\x00" + b"\x00" * 8)
    n = fx.call("nota_store_load", fx.cstr("/data/nota/bad.db"))
    # Fx.call returns raw bits; a negative STORE_ERR_* comes back as a huge
    # unsigned value, so compare through s64.
    n = n - (1 << 64) if n >> 63 else n
    assert n < 0, f"corrupt file loaded {n} notes"


@test
def test_save_clears_the_dirty_flag(fx):
    seed_notes(fx, [b"one\nbody"])
    st = fx.sym("nota_store")
    fx.set_u64(st + 0x20, 1)
    fx.call("nota_store_save", fx.cstr("/data/nota/n.db"))
    assert fx.u64(st + 0x20) == 0, "a successful save must clear DIRTY"


def main():
    verbose = "-v" in sys.argv
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    elf, syms = build()
    fails = 0
    ran = 0
    for fn in TESTS:
        if names and not any(n in fn.__name__ for n in names):
            continue
        ran += 1
        fx = Fx(elf, syms)
        try:
            fn(fx)
            print(f"  PASS  {fn.__name__}")
        except Exception:
            fails += 1
            print(f"  FAIL  {fn.__name__}")
            if verbose:
                traceback.print_exc()
            else:
                print("        " + traceback.format_exc().strip().splitlines()[-1])
    print(f"\n{ran - fails}/{ran} UI tests passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
