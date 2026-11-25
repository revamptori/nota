#!/usr/bin/env python3
"""arsc.py - build resources.arsc + a launcher icon for a pure-native APK.

Build tooling only. Emits the same ResTable structure aapt2 produces:

  ResTable_header { ResChunk_header; uint32 packageCount }
    ResStringPool (global value strings; UTF-8 in practice is accepted,
                   we use UTF-16 to match aapt2 for resource *values*)
    ResTable_package {
        ResChunk_header; uint32 id; uint16 name[128]; uint32 typeStrings;
        uint32 lastPublicType; uint32 keyStrings; uint32 lastPublicKey;
        uint32 typeIdOffset;                       // headerSize 288 (or 284)
    }
      ResStringPool (type names)
      ResStringPool (key names)
      ResTable_typeSpec { header; uint8 id; pad; uint16 entryCount; uint32 configs[] }
      ResTable_type     { header; uint8 id; flags; uint32 entryCount;
                          uint32 entriesStart; ResTable_config config;
                          uint32 entryOffsets[] ; entries... }
"""
import struct
import zlib

RES_STRING_POOL_TYPE = 0x0001
RES_TABLE_TYPE = 0x0002
RES_TABLE_PACKAGE_TYPE = 0x0200
RES_TABLE_TYPE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC_TYPE = 0x0202

UTF8_FLAG = 1 << 8
NO_ENTRY = 0xFFFFFFFF
FLAG_COMPLEX = 0x0001
FLAG_SPARSE = 0x01  # in ResTable_type.flags

TYPE_REFERENCE = 0x01

# A default ResTable_config: size == 64 on API 34 (includes grammatical gender,
# color mode, locale, etc.). Android only reads `size` bytes.
CONFIG_HEADER_SIZE = 64


def chunk(ctype, payload, header_size=8, pre=None):
    if pre is None:
        pre = b"\x00" * (header_size - 8)
    assert len(pre) == header_size - 8, (len(pre), header_size)
    return struct.pack("<HHI", ctype, header_size, header_size + len(payload)) + pre + payload


class StringPool16:
    """UTF-16 ResStringPool (used for resource values and package key strings)."""

    def __init__(self):
        self.strings, self._idx = [], {}

    def add(self, s):
        if s not in self._idx:
            self._idx[s] = len(self.strings)
            self.strings.append(s)
        return self._idx[s]

    def encode(self, sort=False):
        order = range(len(self.strings))
        data = bytearray()
        offsets = []
        for i in order:
            offsets.append(len(data))
            enc = self.strings[i].encode("utf-16-le")
            data += struct.pack("<H", len(enc) // 2) + enc + b"\x00\x00"
        while len(data) % 4:
            data += b"\x00"
        header_size = 28
        strings_start = header_size + 4 * len(self.strings)
        out = struct.pack("<HHI", RES_STRING_POOL_TYPE, header_size,
                          strings_start + len(data))
        out += struct.pack("<IIIII", len(self.strings), 0, 0, strings_start, 0)
        out += b"".join(struct.pack("<I", o) for o in offsets)
        return out + bytes(data)


def default_config():
    # ResTable_config: uint32 size; then locale/qualifier fields, all default.
    return struct.pack("<I", CONFIG_HEADER_SIZE) + b"\x00" * (CONFIG_HEADER_SIZE - 4)


def build_arsc(package_name="com.revamptori.nota", package_id=0x7F,
               icon_res_id=0x7F020000, string_label_id=0x7F030000,
               label="NOTA"):
    """Resources:
        0x7f02/0000  mipmap/ic_launcher -> res/mipmap/ic_launcher.png
        0x7f03/0000  string/app_name   -> "NOTA"
    Type ids: 0x02 mipmap, 0x03 string (matching aapt2's typical order is not
    required; only consistency inside this table matters).
    """
    # (type name, type id, [(entry index, key, value pool index, key pool index)])
    types = [("mipmap", 0x02, [(0, "ic_launcher", 0, 0)]),
             ("string", 0x03, [(0, "app_name", 1, 1)])]

    type_pool = StringPool16()
    for name, _, _ in types:
        type_pool.add(name)
    key_pool = StringPool16()
    for _, _, keys in types:
        for _, k, _, _ in keys:
            key_pool.add(k)
    value_pool = StringPool16()
    value_pool.add("res/mipmap/ic_launcher.png")
    value_pool.add(label)

    def type_spec(tid, count):
        # header: {chunk} + id(1) + res0(1) + res1(2) + entryCount(4) = 16
        return chunk(RES_TABLE_TYPE_SPEC_TYPE,
                     struct.pack("<I", 0) * count,
                     header_size=16,
                     pre=struct.pack("<BBHI", tid, 0, 0, count))

    def type_chunk(tid, entries):
        """entries: list of (index, bytes). ResTable_type layout:
           uint16 header, uint8 id, uint8 flags, uint32 reserved,
           uint32 entryCount, uint32 entriesStart, ResTable_config(64),
           uint32 entryOffsets[entryCount]
        """
        count = max(i for i, _ in entries) + 1
        # headerSize = 8(chunk)+4(id/flags/res)+4(count)+4(entriesStart)+config
        header_size = 20 + CONFIG_HEADER_SIZE  # 84
        offsets = [0xFFFFFFFF] * count
        blob = bytearray()
        for idx, data in entries:
            while len(blob) % 4:
                blob += b"\x00"
            offsets[idx] = len(blob)
            blob += data
        # chunk() already emitted {type, headerSize, size} - 8 bytes. What
        # follows in ResTable_type is id, flags, reserved, entryCount,
        # entriesStart, then the config. An earlier version began this with
        # another uint16 header_size, which shifted every field by four bytes:
        # the parser read headerSize (84) as the type id and the reserved word
        # as the entry count, so the resource table was structurally invalid.
        header = struct.pack("<BBHII", tid, 0, 0, count,
                             header_size + 4 * count) + default_config()
        entry_offsets = b"".join(struct.pack("<I", o) for o in offsets)
        return chunk(RES_TABLE_TYPE_TYPE, entry_offsets + bytes(blob),
                     header_size=header_size, pre=header)

    def value_entry(value_index, key_index):
        # ResTable_entry { uint16 size=8; uint16 flags; uint32 key; }
        # Res_value     { uint16 size=8; uint8 res0; uint8 dataType; uint32 data; }
        return struct.pack("<HHI", 8, 0, key_index) + struct.pack(
            "<HBBI", 8, 0, 0x03, value_index)

    chunks = b""
    for tname, tid, keys in types:
        chunks += type_spec(tid, max(i for i, *_ in keys) + 1)
    for tname, tid, keys in types:
        chunks += type_chunk(tid, [(idx, value_entry(val, kidx))
                                   for idx, _k, val, kidx in keys])
    assert len(chunks) > 0

    type_pool_bytes = type_pool.encode()
    key_pool_bytes = key_pool.encode()

    pkg_header_size = 288
    body = struct.pack("<I", package_id)
    name = package_name.encode("utf-16-le")[:254]
    body += name + b"\x00" * (256 - len(name))
    body += struct.pack("<IIII", pkg_header_size, len(types),
                        pkg_header_size + len(type_pool_bytes), len(key_pool.strings))
    body += struct.pack("<I", 0)  # typeIdOffset
    assert len(body) == pkg_header_size - 8, (len(body), pkg_header_size)
    package = chunk(RES_TABLE_PACKAGE_TYPE,
                    type_pool_bytes + key_pool_bytes + chunks,
                    header_size=pkg_header_size, pre=body)

    values = value_pool.encode()
    # ResTable_header { ResChunk_header; uint32 packageCount }
    # The package count belongs in the 12-byte chunk header, not the payload.
    return chunk(RES_TABLE_TYPE, values + package, header_size=12,
                 pre=struct.pack("<I", 1))


# ------------------------------------------------------------------ icons ----
def make_icon_png(size=192, bg=(0x1B, 0x1E, 0x22), fg=(0xF2, 0xC9, 0x4C)):
    """A tiny launcher icon drawn without any image library.

    Design: rounded dark square, three text lines, one accent line - the
    visual identity of a notes app, encoded as a PNG (RGBA, filter 0).
    """
    W = H = size
    px = bytearray()
    r = size // 4  # corner radius
    for y in range(H):
        for x in range(W):
            # rounded-rect mask
            cx = min(max(x, r), W - 1 - r)
            cy = min(max(y, r), H - 1 - r)
            dx, dy = x - cx, y - cy
            inside = dx * dx + dy * dy <= r * r
            if inside:
                c = bg
                # "paper" area
                m = size // 8
                if m <= x < W - m and m <= y < H - m:
                    c = (0xF7, 0xF5, 0xF0)
                    line_y = [int(H * 0.28), int(H * 0.42), int(H * 0.56)]
                    tw = int(W * 0.52)
                    for i, ly in enumerate(line_y):
                        th = max(2, size // 32)
                        x0 = m + size // 12
                        x1 = x0 + (tw if i != 2 else int(tw * 0.6))
                        if ly <= y < ly + th and x0 <= x < x1:
                            c = (0x6B, 0x70, 0x78)
                    # accent line
                    ay = int(H * 0.70)
                    th = max(3, size // 24)
                    x0 = m + size // 12
                    x1 = x0 + int(W * 0.40)
                    if ay <= y < ay + th and x0 <= x < x1:
                        c = fg
                px += bytes(c) + b"\xff"
            else:
                px += b"\x00\x00\x00\x00"

    raw = bytearray()
    stride = W * 4
    for y in range(H):
        raw += b"\x00" + px[y * stride:(y + 1) * stride]

    def png_chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr)
            + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + png_chunk(b"IEND", b""))


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--arsc", required=True)
    p.add_argument("--icon", required=True)
    p.add_argument("--package", default="com.revamptori.nota")
    p.add_argument("--label", default="NOTA")
    p.add_argument("--icon-size", type=int, default=192)
    a = p.parse_args()
    open(a.arsc, "wb").write(build_arsc(a.package, label=a.label))
    open(a.icon, "wb").write(make_icon_png(a.icon_size))
    print(f"wrote {a.arsc} + {a.icon}")
