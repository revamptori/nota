#!/usr/bin/env python3
"""zipw.py - minimal, deterministic ZIP writer with per-entry alignment.

Build tooling only. Android requires uncompressed (STORED) native libraries
whose data offset is page-aligned; since Android 15 (16 KB pages) that means
16 KiB. aapt2/zipalign express this as "-p 4096 / -P 16", implemented by
padding the local file header's extra field. This writer does the same.

Deterministic output: fixed timestamps, fixed order, no extra fields except
alignment padding, so identical inputs produce identical APKs (reproducible
builds).
"""
import struct
import zlib

DOS_TIME = 0
DOS_DATE = (0 << 9) | (1 << 5) | 1  # 1980-01-01

STORED, DEFLATED = 0, 8


class Entry:
    __slots__ = ("name", "data", "method", "align", "mode", "crc", "csize",
                 "usize", "offset")

    def __init__(self, name, data, method=DEFLATED, align=1, mode=0o644):
        self.name = name
        self.data = data
        self.method = method
        self.align = align
        self.mode = mode

    def compressed(self):
        if self.method == STORED:
            return self.data
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        return c.compress(self.data) + c.flush()


def build(entries, comment=b""):
    """Serialize entries (in the given order) to ZIP bytes."""
    out = bytearray()
    central = bytearray()
    for e in entries:
        assert e.align in (1, 2, 4, 8, 16, 4096, 16384, 65536), e.align
        name = e.name.encode("utf-8")
        body = e.compressed()
        crc = zlib.crc32(e.data) & 0xFFFFFFFF
        extra = b""
        # Local header: 30 bytes + name + extra, then data. An extra field is
        # itself 4 bytes of header plus payload, so whatever padding is chosen
        # has to leave the data at a multiple of `align` *after* the field is
        # included - hence solving for the total, not for the payload.
        if e.align > 1:
            base = len(out) + 30 + len(name)
            pad = (-base) % e.align
            if 0 < pad < 4:
                # a 4-byte field cannot hold fewer than 4 bytes of header
                pad += e.align
            if pad:
                extra = struct.pack("<HH", 0xD935, pad - 4) + b"\x00" * (pad - 4)
        flags = 0
        ver = 20
        e.crc, e.csize, e.usize, e.offset = crc, len(body), len(e.data), len(out)
        out += struct.pack("<IHHHHHIIIHH", 0x04034B50, ver, flags, e.method,
                           DOS_TIME, DOS_DATE, crc, len(body), len(e.data),
                           len(name), len(extra))
        out += name + extra
        assert (len(out) % e.align) == 0, (e.name, len(out), e.align)
        out += body
        central += struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, ver, ver, flags,
                               e.method, DOS_TIME, DOS_DATE, crc, len(body),
                               len(e.data), len(name), 0, 0, 0, 0,
                               (e.mode & 0xFFFF) << 16 | (0o100000 if e.mode & 0o400 else 0), e.offset)
        central += name

    cd_start = len(out)
    out += central
    out += struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries),
                       len(central), cd_start, len(comment))
    out += comment
    return bytes(out)


def read(path):
    """Read a ZIP's entries as (name, bytes) - used by verification tools."""
    import io
    import zipfile

    z = zipfile.ZipFile(path)
    return [(i.filename, z.read(i.filename)) for i in z.infolist()]
