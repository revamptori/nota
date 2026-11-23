"""sysemu.py - a host-side implementation of the Linux syscalls NOTA uses.

This is a test double for the kernel, not part of the app: the runner links the
real `src/sys.S` and executes the real `svc #0` instructions, and this module
answers them. That means the syscall *numbers*, argument order and return-value
handling in sys.S are all under test, and file I/O in the store can be tested
end to end without a device.

Only the calls NOTA makes are implemented. Anything else raises, so an
unexpected syscall shows up as a test failure instead of a silent no-op.
"""
import errno
import os
import struct
import tempfile

AT_FDCWD = -100
O_RDONLY, O_WRONLY, O_RDWR = 0, 1, 2
O_CREAT, O_TRUNC, O_APPEND = 64, 512, 1024
SEEK_SET, SEEK_CUR, SEEK_END = 0, 1, 2

SYS_GETCWD, SYS_MKDIRAT, SYS_UNLINKAT = 17, 34, 35
SYS_RENAMEAT = 38
SYS_FTRUNCATE, SYS_OPENAT, SYS_CLOSE = 46, 56, 57
SYS_GETDENTS64, SYS_LSEEK, SYS_READ, SYS_WRITE = 61, 62, 63, 64
SYS_FSTATAT, SYS_FSYNC, SYS_EXIT_GROUP = 79, 82, 94
SYS_CLOCK_NANOSLEEP, SYS_CLOCK_GETTIME = 115, 113
SYS_GETPID = 172


class SyscallError(Exception):
    pass


class Kernel:
    """A tiny in-memory filesystem plus the syscalls that touch it.

    Paths are virtual: `"/data"` is the app's private directory. Files live in a
    dict so tests can inspect them directly (`k.files["/data/x"]`), and a
    "crash" can be simulated by raising after N write syscalls.
    """

    def __init__(self):
        self.files = {}
        self.dirs = {"/", "/data", "/data/nota", "/sdcard"}
        self.fds = {}            # fd -> [path, pos, flags, buf]
        self.next_fd = 3
        self.now_ms = 1000
        self.log = []            # (name, args) for assertions
        self.write_allowed = None  # int: fail after this many writes (0 = immediately)

    # ------------------------------------------------------------- helpers --
    def _norm(self, path):
        if isinstance(path, bytes):
            path = path.decode()
        if not path.startswith("/"):
            path = "/" + path
        parts = []
        for p in path.split("/"):
            if p in ("", "."):
                continue
            if p == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(p)
        return "/" + "/".join(parts)

    def _err(self, e):
        return -int(e)

    def _read_cstr(self, m, addr):
        out = bytearray()
        while True:
            b = m.load_mem(addr, 1)[0]
            if b == 0:
                return out.decode("utf-8", "surrogateescape")
            out.append(b)
            addr += 1

    def _new_fd(self, entry):
        fd = self.next_fd
        self.next_fd += 1
        self.fds[fd] = entry
        return fd

    def __call__(self, m):
        """Service the `svc #0` at m.pc. Returns the next PC."""
        nr = m.x[8]
        a0, a1, a2, a3, a4, a5 = (m.x[i] for i in range(6))
        handler = self.TABLE.get(nr)
        if handler is None:
            raise SyscallError(f"unimplemented syscall {nr} "
                               f"(x0={a0:#x} x1={a1:#x})")
        r = handler(self, m, a0, a1, a2, a3, a4, a5)
        m.x[0] = r & 0xFFFFFFFFFFFFFFFF
        return m.pc + 4

    # ----------------------------------------------------------- syscalls --
    def s_getpid(self, m, *a):
        return 4242

    def s_clock_gettime(self, m, clk, ts, *rest):
        sec, nsec = divmod(self.now_ms, 1000)
        m.store_mem(ts, struct.pack("<qq", sec, nsec * 1000000))
        return 0

    def s_clock_nanosleep(self, m, clk, flags, req, rem, *rest):
        sec, nsec = struct.unpack("<qq", m.load_mem(req, 16))
        self.now_ms += sec * 1000 + nsec // 1000000
        return 0

    def s_openat(self, m, dirfd, path, flags, mode, *rest):
        p = self._norm(self._read_cstr(m, path))
        self.log.append(("open", p, flags))
        exists = p in self.files
        if not exists:
            if not (flags & O_CREAT):
                return self._err(errno.ENOENT)
            if p.rsplit("/", 1)[0] not in self.dirs:
                return self._err(errno.ENOENT)
            self.files[p] = bytearray()
        elif flags & O_TRUNC:
            self.files[p] = bytearray()
        return self._new_fd({"path": p, "pos": 0, "flags": flags})

    def s_read(self, m, fd, buf, n, *rest):
        e = self.fds.get(fd)
        if e is None:
            return self._err(errno.EBADF)
        data = self.files[e["path"]]
        chunk = bytes(data[e["pos"]:e["pos"] + n])
        if not chunk:
            return 0
        m.store_mem(buf, chunk)
        e["pos"] += len(chunk)
        return len(chunk)

    def s_write(self, m, fd, buf, n, *rest):
        e = self.fds.get(fd)
        if e is None or not (e["flags"] & (O_WRONLY | O_RDWR)):
            return self._err(errno.EBADF)
        if self.write_allowed is not None:
            if self.write_allowed <= 0:
                return self._err(errno.ENOSPC)
            self.write_allowed -= 1
        data = bytearray(m.load_mem(buf, n))
        f = self.files[e["path"]]
        pos = len(f) if e["flags"] & O_APPEND else e["pos"]
        if pos > len(f):
            f.extend(bytearray(pos - len(f)))
        f[pos:pos + n] = data
        e["pos"] = pos + n
        return n

    def s_close(self, m, fd, *rest):
        if fd not in self.fds:
            return self._err(errno.EBADF)
        del self.fds[fd]
        return 0

    def s_lseek(self, m, fd, off, whence, *rest):
        e = self.fds.get(fd)
        if e is None:
            return self._err(errno.EBADF)
        size = len(self.files[e["path"]])
        if whence == SEEK_SET:
            e["pos"] = off
        elif whence == SEEK_CUR:
            e["pos"] += off
        else:
            e["pos"] = size + off
        return e["pos"]

    def s_ftruncate(self, m, fd, size, *rest):
        e = self.fds.get(fd)
        if e is None:
            return self._err(errno.EBADF)
        f = self.files[e["path"]]
        del f[size:]
        return 0

    def s_fsync(self, m, fd, *rest):
        return 0 if fd in self.fds else self._err(errno.EBADF)

    def s_renameat(self, m, ofd, old, nfd, new, *rest):
        o = self._norm(self._read_cstr(m, old))
        n = self._norm(self._read_cstr(m, new))
        self.log.append(("rename", o, n))
        if o not in self.files:
            return self._err(errno.ENOENT)
        if n.rsplit("/", 1)[0] not in self.dirs:
            return self._err(errno.ENOENT)
        self.files[n] = self.files.pop(o)
        return 0

    def s_unlinkat(self, m, fd, path, flags, *rest):
        p = self._norm(self._read_cstr(m, path))
        if p not in self.files:
            return self._err(errno.ENOENT)
        del self.files[p]
        return 0

    def s_mkdirat(self, m, fd, path, mode, *rest):
        p = self._norm(self._read_cstr(m, path))
        if p in self.dirs:
            return self._err(errno.EEXIST)
        if p.rsplit("/", 1)[0] not in self.dirs:
            return self._err(errno.ENOENT)
        self.dirs.add(p)
        return 0

    def s_fstatat(self, m, dirfd, path, st, flags, *rest):
        p = self._norm(self._read_cstr(m, path))
        if p in self.files:
            size, mode = len(self.files[p]), 0o100000 | 0o644
        elif p in self.dirs:
            size, mode = 0, 0o040000 | 0o755
        else:
            return self._err(errno.ENOENT)
        st_buf = bytearray(256)
        struct.pack_into("<Q", st_buf, 0, 0)        # st_dev
        struct.pack_into("<Q", st_buf, 8, 1)        # st_ino
        struct.pack_into("<I", st_buf, 16, mode)    # st_mode
        struct.pack_into("<Q", st_buf, 48, size)    # st_size
        m.store_mem(st, bytes(st_buf))
        return 0

    def s_getcwd(self, m, buf, size, *rest):
        cwd = b"/data\0"
        m.store_mem(buf, cwd)
        return len(cwd)

    def s_getdents64(self, m, fd, buf, n, *rest):
        e = self.fds.get(fd)
        if e is None:
            return self._err(errno.EBADF)
        d = e["path"]
        entries = ["."] + ([".."] if d != "/" else []) + sorted(
            p[len(d) + 1:].split("/")[0] for p in list(self.files) + list(self.dirs)
            if p.startswith(d + "/") and p != d)
        out = bytearray()
        start = e["pos"]
        for i, name in enumerate(entries[start:], start):
            d_type = 4 if (d.rstrip("/") + "/" + name) in self.dirs else 8
            rec = struct.pack("<QqHB", (i + 1) * 1024, i, 19 + len(name) + 1, d_type)
            rec += name.encode() + b"\0"
            rec += b"\0" * ((8 - len(rec) % 8) % 8)
            if len(out) + len(rec) > n:
                break
            out += rec
            e["pos"] = i + 1
        m.store_mem(buf, bytes(out))
        return len(out)

    def s_exit_group(self, m, code, *rest):
        raise SyscallError(f"exit_group({code}) - the test program exited")

    TABLE = {
        SYS_GETCWD: s_getcwd,
        SYS_GETPID: s_getpid,
        SYS_CLOCK_GETTIME: s_clock_gettime,
        SYS_CLOCK_NANOSLEEP: s_clock_nanosleep,
        SYS_OPENAT: s_openat,
        SYS_READ: s_read,
        SYS_WRITE: s_write,
        SYS_CLOSE: s_close,
        SYS_LSEEK: s_lseek,
        SYS_FTRUNCATE: s_ftruncate,
        SYS_FSYNC: s_fsync,
        SYS_RENAMEAT: s_renameat,
        SYS_UNLINKAT: s_unlinkat,
        SYS_MKDIRAT: s_mkdirat,
        SYS_FSTATAT: s_fstatat,
        SYS_GETDENTS64: s_getdents64,
        SYS_EXIT_GROUP: s_exit_group,
    }
