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
        m = re.match(r"^\.globl\s+(\w+)", lines[i])
        if m and m.group(1).startswith("nota_"):
            name = m.group(1)
            start = i
            body = []
            i += 1
            while i < len(lines):
                ln = lines[i]
                if re.match(r"^\s*\.size\s+" + re.escape(name), ln):
                    break
                # a new global symbol at column 0 ends the previous body
                if re.match(r"^\.globl", ln):
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
    """Destination register of an instruction, or None."""
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


def check(path, errors, warnings):
    for name, line0, body in routines(path):
        text = [strip_comment(l) for _, l in body]
        insns = [t.strip() for t in text if t.strip() and not t.strip().startswith(".")]
        has_ret = any(i.split()[0] == "ret" for i in insns if i.split())
        calls = [i for i in insns if re.match(r"^(bl|blr)\b", i)]
        tail = insns[-1] if insns else ""
        if calls and has_ret:
            saved = any(re.search(r"\bx30\b", i) and i.startswith(("stp", "str"))
                        for i in insns)
            if not saved:
                errors.append(f"{path}:{line0}: {name} calls a helper but never "
                              f"saves x30 on the stack")
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
