#!/usr/bin/env python3
"""test_resources.py - structural checks on the compiled resources.arsc.

resources.arsc is written by hand in tools/arsc.py, and getting it wrong is a
package that will not parse: Android validates the chunk tree before the
signature, so the "There was a problem parsing the package" dialog often means
the resource table, not the manifest.

It has already happened once. ResTable_type was emitted with an extra header
size word, shifting id/flags/reserved by four bytes, so the parser read the
header size (84) as the type id and the reserved word as the entry count. Every
chunk after it was then walked at the wrong offset.

These tests read the built table the way a parser does - trusting only the
declared chunk sizes - and check the fields that have to agree with each other.

Usage:  python3 tests/test_resources.py [-v]
"""
import os
import struct
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import arsc                                     # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# chunk types
RES_STRING_POOL = 0x0001
RES_TABLE = 0x0002
RES_TABLE_PACKAGE = 0x0200
RES_TABLE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC = 0x0202

RES_XML_RESOURCE_MAP = 0x0180

NO_ENTRY = 0xFFFFFFFF


def _table():
    """The same table build.py emits: two types, one entry each."""
    if "t" not in _table.cache:
        _table.cache["t"] = arsc.build_arsc(label="NOTA")
    return _table.cache["t"]


_table.cache = {}


def chunks(data, start=0, end=None):
    """Yield (type, header_size, size, offset), trusting declared sizes only."""
    off = start
    end = len(data) if end is None else end
    while off + 8 <= end:
        ctype, hsize, size = struct.unpack_from("<HHI", data, off)
        yield ctype, hsize, size, off
        if size < 8:
            raise AssertionError(f"chunk at {off} has size {size}")
        off += size


# -------------------------------------------------------------------- tests --
@test
def test_table_header():
    d = _table()
    ctype, hsize, size = struct.unpack_from("<HHI", d, 0)
    assert ctype == RES_TABLE, hex(ctype)
    assert size == len(d), (size, len(d))
    count, = struct.unpack_from("<I", d, 8)
    assert count == 1, count


@test
def test_top_level_chunks_tile_the_file_exactly():
    """Every chunk must end where the next begins, with nothing left over.

    This is the check that fails when a struct is emitted with the wrong
    number of fields: the declared size no longer matches the layout and the
    walk lands mid-structure.
    """
    d = _table()
    seen = list(chunks(d, start=12))       # skip the 12-byte table header
    assert seen, "no chunks after the table header"
    end = 12 + sum(size for _t, _h, size, _o in seen)
    assert end == len(d), f"chunks cover {end} of {len(d)} bytes"


@test
def test_package_chunk_is_consistent():
    d = _table()
    pkgs = [c for c in chunks(d, start=12) if c[0] == RES_TABLE_PACKAGE]
    assert len(pkgs) == 1, [hex(c[0]) for c in chunks(d, start=12)]
    _t, hsize, size, off = pkgs[0]
    assert off + size == len(d), (off + size, len(d))
    pid, = struct.unpack_from("<I", d, off + 8)
    assert pid == 0x7F, hex(pid)


@test
def test_type_spec_and_type_chunks_are_well_formed():
    """ResTable_typeSpec is {header; id; res0; res1; entryCount} = 20 bytes,
    ResTable_type is {header; id; flags; reserved; entryCount; entriesStart;
    config} = 20 + sizeof(ResTable_config).

    The failure this guards against read the header size as the type id, so
    assert both that ids are sane (1..255, small) and that res0/res1 are zero.
    """
    d = _table()
    pkgs = [c for c in chunks(d, start=12) if c[0] == RES_TABLE_PACKAGE]
    _t, hsize, size, off = pkgs[0]

    specs, types = [], []
    for ctype, chs, csize, coff in chunks(d, off + hsize, off + size):
        if ctype == RES_TABLE_TYPE_SPEC:
            specs.append((chs, csize, coff))
        elif ctype == RES_TABLE_TYPE:
            types.append((chs, csize, coff))

    assert specs, "no ResTable_typeSpec chunks"
    assert types, "no ResTable_type chunks"

    for chs, csize, coff in specs:
        tid = d[coff + 8]
        res0 = d[coff + 9]
        res1, = struct.unpack_from("<H", d, coff + 10)
        count, = struct.unpack_from("<I", d, coff + 12)
        assert chs == 16, f"typeSpec headerSize {chs}"
        assert 1 <= tid < 0x40, f"typeSpec id looks like a header size: {tid}"
        assert res0 == 0 and res1 == 0, (res0, res1)
        assert csize == chs + 4 * count, (csize, chs, count)

    for chs, csize, coff in types:
        tid = d[coff + 8]
        flags = d[coff + 9]
        res1, = struct.unpack_from("<H", d, coff + 10)
        count, start = struct.unpack_from("<II", d, coff + 12)
        cfg_size, = struct.unpack_from("<I", d, coff + 20)
        assert 1 <= tid < 0x40, f"type id looks like a header size: {tid}"
        assert flags == 0, hex(flags)
        assert res1 == 0, res1
        assert chs == 20 + cfg_size, (chs, cfg_size)
        assert cfg_size == 64, cfg_size
        assert start >= chs + 4 * count, (start, chs, count)


@test
def test_type_spec_ids_match_type_ids():
    """Every type must have a spec with the same id, or lookups misbehave."""
    d = _table()
    pkgs = [c for c in chunks(d, start=12) if c[0] == RES_TABLE_PACKAGE]
    _t, hsize, size, off = pkgs[0]
    spec_ids, type_ids = [], []
    for ctype, _chs, _cs, coff in chunks(d, off + hsize, off + size):
        if ctype == RES_TABLE_TYPE_SPEC:
            spec_ids.append(d[coff + 8])
        elif ctype == RES_TABLE_TYPE:
            type_ids.append(d[coff + 8])
    assert sorted(spec_ids) == sorted(type_ids), (spec_ids, type_ids)
    assert len(set(type_ids)) == len(type_ids), f"duplicate type ids {type_ids}"


@test
def test_entries_resolve_to_strings():
    """The icon and label entries must point at real string pool indices."""
    d = _table()
    # global string pool: first chunk after the table header
    _ct, _hs, _sz, pool_off = next(chunks(d, start=12))
    strings = _string_pool(d, pool_off)

    pkgs = [c for c in chunks(d, start=12) if c[0] == RES_TABLE_PACKAGE]
    _t, hsize, size, off = pkgs[0]

    found = []
    for ctype, chs, _cs, coff in chunks(d, off + hsize, off + size):
        if ctype != RES_TABLE_TYPE:
            continue
        count, start = struct.unpack_from("<II", d, coff + 12)
        offsets = struct.unpack_from("<%dI" % count, d, coff + chs)
        for k, rel in enumerate(offsets):
            if rel == NO_ENTRY:
                continue
            e = coff + start + rel
            esize, _eflags, _ekey = struct.unpack_from("<HHI", d, e)
            assert esize == 8, f"ResTable_entry size {esize}"
            # Res_value starts right after the entry
            vsize, _res0, vtype, vdata = struct.unpack_from("<HBBI", d,
                                                           e + esize)
            assert vsize == 8, f"Res_value size {vsize}"
            if vtype == 0x03:                # TYPE_STRING
                assert vdata < len(strings), (
                    f"type {d[coff+8]} entry {k} -> string {vdata} "
                    f"of {len(strings)}")
                found.append(strings[vdata])

    assert "res/mipmap/ic_launcher.png" in found, found
    assert "NOTA" in found, found


def _string_pool(data, off):
    count, _styles, flags, strings_start, _ss = struct.unpack_from(
        "<IIIII", data, off + 8)
    utf8 = bool(flags & 0x100)
    base = off + strings_start
    out = []
    for i in range(count):
        rel, = struct.unpack_from("<I", data, off + 28 + i * 4)
        p = base + rel
        if utf8:
            def vi(p):
                b = data[p]
                if b & 0x80:
                    return ((b & 0x7F) << 8) | data[p + 1], p + 2
                return b, p + 1
            _c, p = vi(p)
            n, p = vi(p)
            out.append(data[p:p + n].decode("utf-8", "replace"))
        else:
            n, = struct.unpack_from("<H", data, p)
            p += 2
            out.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
    return out


def main():
    verbose = "-v" in sys.argv
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    passed = failed = 0
    for fn in TESTS:
        if only and not any(o in fn.__name__ for o in only):
            continue
        try:
            fn()
            passed += 1
            if verbose:
                print("  PASS ", fn.__name__)
        except Exception as e:
            failed += 1
            print("  FAIL ", fn.__name__)
            if verbose:
                traceback.print_exc()
            else:
                print("        %s: %s" % (type(e).__name__, e))
    print("\n%d/%d resource tests passed" % (passed, passed + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
