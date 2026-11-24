#!/usr/bin/env python3
"""checkabi.py - static AAPCS64 checks on NOTA assembly sources.

The interpreter caught a class of bugs that are easy to reintroduce and hard to
see: a routine writing a *live argument* register, and a routine that calls
another routine without preserving LR. Both are mechanical to detect, so they
are checked here instead of by eye.

Rules enforced per routine (a `.globl` symbol at column 0, ending at `ret`):

 1. LR preservation: if the routine body contains `bl`, `blr` or calls a
    `nota_*` name via `b`/`bl`, the routine must save x30 (or use a tail `b`
    to another function as its last instruction).
 2. Callee-saved registers: x19..x28 written by a routine must be saved.
 3. Stack discipline: every `stp ..., [sp, #-N]!` / `sub sp, sp, #N` must be
    matched by the mirrored `ldp ..., [sp], #N` / `add sp, sp, #N`.
 4. Return path: `sp` must be restored on every path that reaches `ret`
    (checked indirectly by 3 plus requiring the routine to end with `ret` or a
    tail `b`).
 5. Argument clobber: for routines whose callers pass arguments in x0..x7, a
    `bl` to a helper while an argument register is still needed is flagged
    when the argument is read again *after* the call without being reloaded
    from a callee-saved register. This is reported as a warning because
    proving it in general needs dataflow; the pattern (write to w0..w7, then
    read after `bl`) is precise enough to be useful.

Exit status: 0 clean, 1 errors, 2 only warnings.
"""
import re
import sys
import glob

ARG = {"x%d" % i for i in range(8)} | {"w%d" % i for i in range(8)}
CALLEE = {"x%d" % i for i in range(19, 29)} | {"w%d" % i for i in range(19, 29)}
ALL_REGS = {"x%d" % i for i in range(31)} | {"w%d" % i for i in range(31)}
ALL_REGS |= {"x%d" % i for i in range(31)}  # distinct 32/64 names


def routines(path):
    """Yield (name, line_no, [instructions]) for each .globl function body."""
    lines = open(path).read().split("\n")
    out = []
    i = 0
    while i < len(lines):
        # Directives may be indented (they are inside function bodies
        # elsewhere in this project); an anchored ^\.globl made the checker
        # silently skip whole files, which is worse than a false positive.
        m = re.match(r"^\s*\.globl\s+(\w+)", lines[i])
        if m and m.group(1).startswith("nota_"):
            name = m.group(1)
            start = i
            body = []
            i += 1
            while i < len(lines):
                ln = lines[i]
                if re.match(r"^\s*\.size\s+" + re.escape(name), ln):
                    break
                # a new global symbol ends the previous body
                if re.match(r"^\s*\.globl", ln):
                    break
                body.append((i + 1, ln))
                i += 1
            out.append((name, start + 1, body))
            continue
        i += 1
    return out


def strip_comment(ln):
    for tok in ("/*", "//", "#@"):
        if tok in ln:
            ln = ln.split(tok)[0]
    return ln.rstrip()


def regs_in(operands):
    return set(re.findall(r"\b([xw]\d{1,2})\b", operands))


def written_reg(insn):
    """Destination register of an instruction, or None.

    The input is stripped first: every instruction in this project is indented
    inside its routine, and an anchored ^(\w+) matched none of them, so this
    returned None for the whole codebase and the callee-saved and stale-argument
    rules below never fired.
    """
    insn = insn.strip()
    m = re.match(r"^(\w+)\s+(.*)$", insn)
    if not m:
        return None
    op, rest = m.group(1), m.group(2)
    if op in ("stp", "str", "stur", "sturb", "strb", "strh", "cbz", "cbnz",
              "cmp", "cmn", "tst", "b", "bl", "ret", "nop"):
        return None
    if op in ("ldp", "ldr", "ldrb", "ldrh", "ldur", "ldurb"):
        first = rest.split(",")[0].strip()
        return first if first in ALL_REGS else None
    first = rest.split(",")[0].strip()
    return first if first in ALL_REGS else None


def reads_after_write(insn, written, pending):
    """Update the set of argument registers that are stale after a call."""
    parts = insn.split(None, 1)
    if not parts:
        return pending
    r = regs_in(parts[1] if len(parts) > 1 else "")
    return pending & r


# Fields declared as u32 in src/nota.h. Loading one with a 64-bit `ldr x` picks
# up whatever the neighbouring field holds, which is how a body length acquired
# a pointer in its high half and a document reported 65535 bytes.
U32_FIELDS = ("NOTE_ID", "NOTE_FLAGS", "NOTE_FOLDER", "NOTE_TAGS",
              "NOTE_TITLE_LEN", "NOTE_BODY_LEN", "UNDO_OFFH", "GL_NVERTS")


def check_scratch_overlaps(path, errors):
    """A local scratch slot must not sit inside a saved callee-saved pair.

    Locals live above the saved registers in a frame, but nothing enforces it:
    `add x1, sp, #76` for a 4-byte out-param landed in the top half of the
    `stp x25, x26, [sp, #64]` pair. Decoding a character then silently rewrote
    the saved register, and the routine returned with a corrupted value that
    only blew up much later.
    """
    for name, _start, body in routines(path):
        saved = []                      # (lo, hi) byte ranges of saved pairs
        for _off, raw in body:
            ln = strip_comment(raw)
            m = re.match(r"^\s*stp\s+.*\[sp,\s*#(\d+)\]!?", ln)
            if m and "x29" in ln and "x30" in ln:
                continue                # the frame record itself
            m = re.match(r"^\s*stp\s+x(\d+),\s*x(\d+),\s*\[sp,\s*#(\d+)\]", ln)
            if m:
                a, b, off = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 19 <= a <= 28 and 19 <= b <= 28:
                    saved.append((off, off + 16))
        if not saved:
            continue
        for off_line, raw in body:
            ln = strip_comment(raw)
            # Only slots that are *written* by something other than the save
            # itself can corrupt a saved register: an out-param base handed to
            # a call (`add x1, sp, #N`) or a direct store. Reloads (`ldp`, `ldr`
            # from the slot) are how the save is undone and are expected.
            if not re.match(r"^\s*(add\s+x\d+,\s*sp,|str|strb|strh|stur)", ln):
                continue
            if re.match(r"^\s*str\s+x\d+,\s*\[sp", ln) or \
               re.match(r"^\s*stp\s+.*\[sp", ln):
                continue
            for m in re.finditer(r"\[sp,\s*#(\d+)\]|^\s*add\s+x\d+,\s*sp,\s*#(\d+)", ln):
                slot = int(m.group(1) or m.group(2))
                for lo, hi in saved:
                    if lo <= slot < hi:
                        errors.append(
                            f"{path}:{off_line}: {name} uses [sp, #{slot}] as a "
                            f"scratch slot inside the saved x-pair at "
                            f"[sp, #{lo}]")
                        break


def check_hidden(path, errors):
    """Every exported data object must also be `.hidden`.

    libnota.so is a PIC shared object, and a `adrp`/`add :lo12:` pair against a
    globally-visible symbol needs a dynamic relocation the static linker will
    not invent. The failure is a link error that reads like a missing -fPIC,
    which sends you looking at the compiler flags instead of the directive.
    Functions are exempt: they are called through their PLT-free local alias.
    """
    lines = [strip_comment(x).strip() for x in open(path).read().split("\n")]
    globls = {}
    hidden = set()
    for i, ln in enumerate(lines):
        m = re.match(r"\.globl\s+(\w+)", ln)
        if m:
            globls[m.group(1)] = i + 1
        m = re.match(r"\.hidden\s+(\w+)", ln)
        if m:
            hidden.add(m.group(1))
    # Only data objects: a function is reached by a local branch, whose
    # relocation is resolved at static link time whether or not it is hidden.
    is_function = {name for name in globls
                   if re.search(r"\.type\s+" + re.escape(name) +
                                r",\s*[@%]function", "\n".join(lines))}
    for name, line in globls.items():
        if name in hidden or name in is_function:
            continue
        errors.append(f"{path}:{line}: {name} is .globl but not .hidden "
                      f"(PIC shared object needs hidden data visibility)")


def check_local_overlaps(path, errors):
    """8-byte stack locals must not overlap each other.

    `str x10, [sp, #72]` followed by `str x9, [sp, #76]` looks like two locals
    and is actually one: the second store rewrites the top half of the first
    value. The failure is silent for small numbers - the low word is still
    right - and only shows up as a garbage high word much later.
    """
    for name, _start, body in routines(path):
        slots = {}
        for off_line, raw in body:
            ln = strip_comment(raw)
            m = re.match(r"^\s*(?:str|stp)\s+x\d+(?:,\s*x\d+)?,\s*\[sp,\s*#(\d+)\]", ln)
            if not m:
                continue
            slot = int(m.group(1))
            slots.setdefault(slot, off_line)
        # a store at s+4 overlapping a store at s is the same problem
        base = sorted({o: l for o, l in slots.items()})
        for i, a in enumerate(base):
            for b in base[i + 1:]:
                if 0 < b - a < 8:
                    errors.append(f"{path}:{slots[b]}: {name} writes [sp, #{b}] "
                                  f"overlapping the 8-byte local at [sp, #{a}]")


def check_field_widths(path, errors):
    for i, raw in enumerate(open(path).read().split("\n"), 1):
        ln = strip_comment(raw).strip()
        m = re.match(r"ldr\s+(x\d+),\s*\[[^]]*#(\w+)\]", ln)
        if m and m.group(2) in U32_FIELDS:
            errors.append(f"{path}:{i}: {m.group(2)} is a u32 but is loaded "
                          f"with a 64-bit ldr into {m.group(1)}")


def check_adrp(path, errors):
    """`adrp` yields a page, not an address.

    Using the result directly as a base gives an address that is wrong by the
    low 12 bits of the symbol, which for a page-aligned .bss object is zero -
    so the bug works on small binaries and appears the moment a symbol moves.
    Every adrp naming a project symbol must be followed by the matching
    `add reg, reg, :lo12:symbol`.
    """
    lines = open(path).read().split("\n")
    for i, raw in enumerate(lines):
        ln = strip_comment(raw).strip()
        m = re.match(r"adrp\s+(\w+),\s*([.\w]+)", ln)
        if not m:
            continue
        reg, sym = m.group(1), m.group(2)
        # Local labels (`.Linv`) are just as likely to live in .rodata or .bss
        # and need the same pairing. Only a plain numeric/NULL operand is
        # skipped.
        if sym.startswith("#") or sym.isdigit():
            continue
        window = [strip_comment(x).strip() for x in lines[i + 1:i + 3]]
        want = f":lo12:{sym}"
        if not any(want in w and reg in w for w in window):
            errors.append(f"{path}:{i+1}: adrp {reg}, {sym} is not followed by "
                          f"`add {reg}, {reg}, {want}` (adrp gives a page)")


def check_sections(path, errors):
    """Every function must be emitted into .text.

    A `.section .rodata` left unclosed swallows the functions that follow it.
    That assembles and links without complaint - the symbols simply point into
    read-only data - and then fails at runtime as a branch into the middle of
    a string table. Nothing else in this checker would notice.

    Data objects legitimately live in .rodata and .bss, so only `%function`
    declarations are checked.
    """
    lines = open(path).read().split("\n")
    decl = {}       # symbol -> section it was declared as a function in
    sec = ".text"
    for n, raw in enumerate(lines, 1):
        ln = strip_comment(raw).strip()
        m = re.match(r"\.section\s+([.\w]+)", ln)
        if m:
            sec = m.group(1)
            continue
        m = re.match(r"\.type\s+(\w+),\s*%function", ln)
        if m and sec != ".text":
            decl[m.group(1)] = (sec, n)
        if ln == ".text":
            sec = ".text"
        elif re.match(r"\.(bss|data)\b", ln):
            sec = "." + ln[1:]
    for name, (sect, n) in decl.items():
        errors.append(f"{path}:{n}: function {name} is emitted into {sect}, "
                      f"not .text (a .section was never closed)")


def check(path, errors, warnings):
    check_scratch_overlaps(path, errors)
    check_hidden(path, errors)
    check_local_overlaps(path, errors)
    check_field_widths(path, errors)
    check_adrp(path, errors)
    check_sections(path, errors)
    for name, line0, body in routines(path):
        text = [strip_comment(l) for _, l in body]
        insns = [t.strip() for t in text if t.strip() and not t.strip().startswith(".")]
        has_ret = any(i.split()[0] == "ret" for i in insns if i.split())
        calls = [i for i in insns if re.match(r"^(bl|blr)\b", i)]
        tail = insns[-1] if insns else ""
        # Link register: `bl` overwrites x30, so any routine that both calls a
        # helper and returns has to have stashed the caller's x30 first.
        # Checking for the literal token "x30" missed `stp x29, x30, ...`,
        # which is how every real frame in this project saves it - so the
        # rule never fired on the bug it was written for.
        lr_saved = any(i.startswith(("stp", "str")) and
                       re.search(r"\bx30\b", i.split("[")[0])
                       for i in insns)
        if calls and has_ret and not lr_saved:
            errors.append(f"{path}:{line0}: {name} calls a helper but never "
                          f"saves x30 on the stack (the caller's return "
                          f"address is destroyed by bl)")
        # A bare `ret` in a routine that makes calls is the same bug seen from
        # the other end: even if x30 were saved, this would not restore it.
        if calls and has_ret and not lr_saved and tail.strip() == "ret":
            errors.append(f"{path}:{line0}: {name} ends in a bare ret after "
                          f"calling helpers, so it returns to its own bl")
        # stack discipline
        pushed = 0
        for off, raw in body:
            ln = strip_comment(raw)
            m = re.search(r"stp\s+.*\[sp,\s*#-(\d+)\]!", ln)
            if m:
                pushed += int(m.group(1))
            m = re.search(r"sub\s+sp,\s*sp,\s*#(\d+)", ln)
            if m:
                pushed += int(m.group(1))
            m = re.search(r"ldp\s+.*\[sp\],\s*#(\d+)", ln)
            if m:
                pushed -= int(m.group(1))
            m = re.search(r"add\s+sp,\s*sp,\s*#(\d+)", ln)
            if m:
                pushed -= int(m.group(1))
            if pushed < 0:
                errors.append(f"{path}:{off}: sp underflow (more popped than "
                              f"pushed) in {name}")
                pushed = 0
            if re.match(r"^\s*ret\b", ln) and pushed != 0:
                errors.append(f"{path}:{off}: {name} returns with {pushed} "
                              f"bytes still pushed")
        if pushed != 0:
            errors.append(f"{path}: {name} leaves {pushed} bytes on the stack")
        # callee-saved registers
        saved_cs = set()
        for ln in insns:
            m = re.search(r"(?:stp|str)\s+(.*?)(?:\[sp|,)", ln)
            if ln.startswith(("stp", "str")):
                for r in re.findall(r"\bx(\d+)\b", ln.split("[")[0]):
                    if 19 <= int(r) <= 28:
                        saved_cs.add("x" + r)
        for off, raw in body:
            ln = strip_comment(raw)
            d = written_reg(ln)
            if d and d in CALLEE:
                base = d.replace("w", "x")
                if base not in saved_cs:
                    errors.append(f"{path}:{off}: {name} writes callee-saved "
                                  f"{d} without saving it")
        # stale argument registers across a call
        pending = set()
        for off, raw in body:
            ln = strip_comment(raw)
            t = ln.strip()
            if not t:
                continue
            parts = t.split(None, 1)
            op = parts[0]
            if op in ("bl", "blr"):
                if pending:
                    warnings.append(f"{path}:{off}: {name} calls with "
                                    f"argument{sorted(pending)} live; they are "
                                    f"clobbered by the call")
                    pending = set()
                continue
            d = written_reg(t)
            if d in ARG:
                pending.add(d)
            else:
                pending &= regs_in(parts[1] if len(parts) > 1 else "")
        if name.endswith("_") or not has_ret and not tail.startswith("b "):
            pass


def main():
    files = sys.argv[1:] or sorted(glob.glob("src/*.S"))
    errors, warnings = [], []
    for f in files:
        check(f, errors, warnings)
    for w in warnings:
        print("warning:", w)
    for e in errors:
        print("error:", e)
    if errors:
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    if warnings:
        print(f"\n0 errors, {len(warnings)} warning(s)")
        return 2
    print(f"clean: {', '.join(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
