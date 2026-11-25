#!/usr/bin/env python3
"""axml.py - compile Android binary XML (AXML).

Build tooling only: NOTA contains no Java/Kotlin, so there is no aapt2 step.

Wire format (frameworks/base/libs/androidfw/include/androidfw/ResourceTypes.h):

  ResChunk_header { uint16 type; uint16 headerSize; uint32 size; }
  ResXMLTree_header { ResChunk_header header; }
  ResStringPool_header {
      ResChunk_header header; uint32 stringCount; uint32 styleCount;
      uint32 flags; uint32 stringsStart; uint32 stylesStart; }   // = 28 bytes
  ResXMLTree_node {
      ResChunk_header header; uint32 lineNumber; ResStringPool_ref comment; }
  ResXMLTree_attrExt {
      ResStringPool_ref ns; ResStringPool_ref name;
      uint16 attributeStart;  // 20
      uint16 attributeSize;   // 20
      uint16 attributeCount;
      uint16 idIndex;         // 0
      uint16 classIndex;      // 0
      uint16 styleIndex;      // 0 }
  ResXMLTree_attribute {
      ResStringPool_ref ns; ResStringPool_ref name;
      ResStringPool_ref rawValue; Res_value { uint16 size; uint8 res0;
      uint8 dataType; uint32 data; } }                          // = 20 bytes
  ResXMLTree_endElementExt { ResStringPool_ref ns; ResStringPool_ref name; }
  ResXMLTree_namespaceExt { ResStringPool_ref prefix; ResStringPool_ref uri; }
"""
import struct

RES_STRING_POOL_TYPE = 0x0001
RES_XML_TYPE = 0x0003
RES_XML_START_NAMESPACE_TYPE = 0x0100
RES_XML_END_NAMESPACE_TYPE = 0x0101
RES_XML_START_ELEMENT_TYPE = 0x0102
RES_XML_END_ELEMENT_TYPE = 0x0103
RES_XML_CDATA_TYPE = 0x0104
RES_XML_RESOURCE_MAP_TYPE = 0x0180

UTF8_FLAG = 1 << 8
NO_ENTRY = 0xFFFFFFFF

TYPE_NULL = 0x00
TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_FLOAT = 0x04
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12

ANDROID_NS = "http://schemas.android.com/apk/res/android"

# Framework attribute ids (android.R.attr) - stable platform ABI.
ATTR_IDS = {
    "theme": 0x01010000, "label": 0x01010001, "icon": 0x01010002,
    "name": 0x01010003, "permission": 0x01010006, "hasCode": 0x0101000C,
    "enabled": 0x0101000E, "debuggable": 0x0101000F, "exported": 0x01010010,
    "screenOrientation": 0x0101001E, "configChanges": 0x0101001F,
    "value": 0x01010024, "resource": 0x01010025, "largeHeap": 0x0101035A,
    "windowSoftInputMode": 0x0101022B,
    "minSdkVersion": 0x0101020C, "versionCode": 0x0101021B,
    "versionName": 0x0101021C, "anyDensity": 0x0101026C,
    "targetSdkVersion": 0x01010270, "allowBackup": 0x01010280,
    "required": 0x0101028E,
    "hardwareAccelerated": 0x010102D3, "supportsRtl": 0x010103AF,
    "extractNativeLibs": 0x010104EA, "usesCleartextTraffic": 0x010104EC,
    "resizeableActivity": 0x010104F6,
    "fullBackupContent": 0x010104EB, "roundIcon": 0x0101052C,
    "appCategory": 0x01010545,
}

# A few framework style ids used by the manifest.
STYLE_MATERIAL_NO_ACTION_BAR = 0x0103022E


def chunk(ctype, payload, header_size=8, pre=None):
    """Emit a ResChunk: {type, headerSize, size} + header extras + payload."""
    if pre is None:
        pre = b"\x00" * (header_size - 8)
    assert len(pre) == header_size - 8, (len(pre), header_size)
    return (struct.pack("<HHI", ctype, header_size, header_size + len(payload))
            + pre + payload)


class StringPool:
    def __init__(self):
        self.strings, self._idx = [], {}

    def add(self, s):
        if s is None:
            s = ""
        if s not in self._idx:
            self._idx[s] = len(self.strings)
            self.strings.append(s)
        return self._idx[s]

    def index(self, s):
        return self._idx[s]

    def encode(self):
        data, offsets = bytearray(), []
        for s in self.strings:
            offsets.append(len(data))
            raw = s.encode("utf-8")
            n16 = len(s.encode("utf-16-le")) // 2
            data += struct.pack("<B", n16) if n16 <= 0x7F else \
                struct.pack("<BB", (n16 >> 8) | 0x80, n16 & 0xFF)
            data += struct.pack("<B", len(raw)) if len(raw) <= 0x7F else \
                struct.pack("<BB", (len(raw) >> 8) | 0x80, len(raw) & 0xFF)
            data += raw + b"\x00"
        while len(data) % 4:
            data += b"\x00"
        header_size = 28
        strings_start = header_size + 4 * len(self.strings)
        out = struct.pack("<HHI", RES_STRING_POOL_TYPE, header_size,
                          strings_start + len(data))
        out += struct.pack("<IIIII", len(self.strings), 0, UTF8_FLAG,
                           strings_start, 0)
        out += b"".join(struct.pack("<I", o) for o in offsets)
        return out + bytes(data)


class Ref:
    def __init__(self, res_id):
        self.res_id = res_id


class AXMLBuilder:
    def __init__(self):
        self.pool = StringPool()
        self.resmap = []
        self.attr_index = {}

    def _collect(self, root):
        order = []

        def walk(el):
            for a in el[1]:
                if a["ns"] == "android":
                    if a["name"] not in ATTR_IDS:
                        raise SystemExit(f"unmapped android attr: {a['name']}")
                    if a["name"] not in order:
                        order.append(a["name"])
            for k in el[2]:
                walk(k)

        walk(root)
        self.attr_order = order

    def build(self, root, package=""):
        self._collect(root)

        # Resource-map entries correspond to string-pool indices, so the
        # android: attribute names must occupy indices 0..N-1. They are added
        # first, before any tag or value string. Adding them after the tags, as
        # this did originally, put them at index 22+ while the resmap stayed
        # 21 entries long: the parser then looked up an attribute name index
        # past the end of the map and rejected the package.
        for nm in self.attr_order:
            idx = self.pool.add(nm)
            assert idx == len(self.resmap), (nm, idx, len(self.resmap))
            self.resmap.append(ATTR_IDS[nm])
            self.attr_index[nm] = idx

        ns_prefix = self.pool.add("android")
        ns_uri = self.pool.add(ANDROID_NS)
        self.pool.add(package)

        def walk_strings(el):
            self.pool.add(el[0])
            for a in el[1]:
                if a["ns"] != "android":
                    self.pool.add(a["name"])
                if isinstance(a["value"], str):
                    self.pool.add(a["value"])
            for k in el[2]:
                walk_strings(k)

        walk_strings(root)

        def node(ctype, payload):
            # ResXMLTree_node header = ResChunk_header(8) + lineNumber(4)
            # + comment(4) = 16 bytes, followed by payload.
            return chunk(ctype, payload, header_size=16,
                         pre=struct.pack("<II", 0, NO_ENTRY))

        def ns_node(ctype):
            # ResXMLTree_namespaceExt { prefix, uri }
            return node(ctype, struct.pack("<II", ns_prefix, ns_uri))

        def value_of(v):
            if isinstance(v, Ref):
                return struct.pack("<HBBI", 8, 0, TYPE_REFERENCE, v.res_id)
            if isinstance(v, bool):
                return struct.pack("<HBBI", 8, 0, TYPE_INT_BOOLEAN,
                                   0xFFFFFFFF if v else 0)
            if isinstance(v, int):
                if v < 0:
                    return struct.pack("<HBBI", 8, 0, TYPE_INT_DEC, v & 0xFFFFFFFF)
                t = TYPE_INT_HEX if v > 0x7FFFFFFF else TYPE_INT_DEC
                return struct.pack("<HBBI", 8, 0, t, v)
            if isinstance(v, float):
                return struct.pack("<HBBI", 8, 0, TYPE_FLOAT,
                                   struct.unpack("<I", struct.pack("<f", v))[0])
            return struct.pack("<HBBI", 8, 0, TYPE_STRING, self.pool.index(v))

        def raw_of(v):
            return self.pool.index(v) if isinstance(v, str) else NO_ENTRY

        nodes = []

        def emit(el):
            name, attrs, kids, text = el
            # 16-byte node header already written by chunk(); payload starts at
            # ResXMLTree_attrExt { ns, name, attributeStart, attributeSize,
            #                      attributeCount, idIndex, classIndex, styleIndex }
            body = struct.pack("<II", NO_ENTRY, self.pool.index(name))
            body += struct.pack("<HHHHHH", 20, 20, len(attrs), 0, 0, 0)
            for a in attrs:
                if a["ns"] == "android":
                    ns = ns_uri
                    nm = self.attr_index[a["name"]]
                else:
                    ns = NO_ENTRY
                    nm = self.pool.index(a["name"])
                body += struct.pack("<III", ns, nm, raw_of(a["value"]))
                body += value_of(a["value"])
            nodes.append(node(RES_XML_START_ELEMENT_TYPE, body))
            if text is not None:
                cdata = struct.pack("<II", self.pool.index(name), NO_ENTRY)
                nodes.append(node(RES_XML_CDATA_TYPE, cdata))
            for k in kids:
                emit(k)
            end = struct.pack("<II", NO_ENTRY, self.pool.index(name))
            nodes.append(node(RES_XML_END_ELEMENT_TYPE, end))

        emit(root)

        body = bytearray()
        body += ns_node(RES_XML_START_NAMESPACE_TYPE)
        for n in nodes:
            body += n
        body += ns_node(RES_XML_END_NAMESPACE_TYPE)

        pool_bytes = self.pool.encode()
        resmap = chunk(RES_XML_RESOURCE_MAP_TYPE,
                       b"".join(struct.pack("<I", i) for i in self.resmap)) \
            if self.resmap else b""
        total = 8 + len(pool_bytes) + len(resmap) + len(body)
        return struct.pack("<HHI", RES_XML_TYPE, 8, total) + pool_bytes + resmap + bytes(body)


def N(name, attrs=None, children=None, text=None):
    out = []
    for k, v in (attrs or {}).items():
        if isinstance(k, tuple):
            ns, nm = k
            if ns not in ("", None, "android"):
                raise SystemExit(f"bad namespace {ns}")
            ns = ns or ""
        elif k in ATTR_IDS:
            ns, nm = "android", k
        else:
            ns, nm = "", k
        out.append({"ns": ns, "name": nm, "value": v})
    return (name, out, list(children or []), text)


def compile_manifest(package="com.revamptori.nota", version_code=1,
                     version_name="1.0.0", min_sdk=26, target_sdk=34,
                     label="NOTA", debug=False, icon_res=None, lib_name="nota",
                     activity_name="android.app.NativeActivity"):
    app_attrs = {
        ("android", "label"): label,
        ("android", "hasCode"): False,
        ("android", "allowBackup"): False,
        ("android", "hardwareAccelerated"): True,
        ("android", "extractNativeLibs"): False,
        ("android", "resizeableActivity"): True,
        ("android", "supportsRtl"): True,
    }
    if icon_res is not None:
        app_attrs[("android", "icon")] = Ref(icon_res)
    if debug:
        app_attrs[("android", "debuggable")] = True

    activity = N("activity", {
        ("android", "name"): activity_name,
        ("android", "exported"): True,
        ("android", "label"): label,
        ("android", "screenOrientation"): "fullUser",
        ("android", "windowSoftInputMode"): "adjustResize",
        ("android", "configChanges"):
            "orientation|screenSize|smallestScreenSize|screenLayout|"
            "keyboardHidden|keyboard|uiMode|density|navigation|"
            "layoutDirection|locale|fontScale|touchscreen",
        ("android", "theme"): Ref(STYLE_MATERIAL_NO_ACTION_BAR),
    }, [
        N("meta-data", {
            ("android", "name"): "android.app.lib_name",
            ("android", "value"): lib_name,
        }),
        # Without this the activity exports fine but appears in no launcher:
        # the app installs and then cannot be started by tapping an icon.
        # ACTION_MAIN + CATEGORY_LAUNCHER is what puts it in the app drawer.
        N("intent-filter", {}, [
            N("action", {("android", "name"): "android.intent.action.MAIN"}),
            N("category", {("android", "name"):
                           "android.intent.category.LAUNCHER"}),
        ]),
    ])

    root = N("manifest", {
        "package": package,
        ("android", "versionCode"): version_code,
        ("android", "versionName"): version_name,
    }, [
        N("uses-sdk", {
            ("android", "minSdkVersion"): min_sdk,
            ("android", "targetSdkVersion"): target_sdk,
        }),
        N("uses-feature", {
            ("android", "name"): "android.hardware.touchscreen",
            ("android", "required"): False,
        }),
        N("application", app_attrs, [activity]),
    ])
    return AXMLBuilder().build(root, package=package)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--package", default="com.revamptori.nota")
    p.add_argument("--version-code", type=int, default=1)
    p.add_argument("--version-name", default="1.0.0")
    p.add_argument("--min-sdk", type=int, default=26)
    p.add_argument("--target-sdk", type=int, default=34)
    p.add_argument("--label", default="NOTA")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--icon-res", type=lambda s: int(s, 0), default=None)
    a = p.parse_args()
    data = compile_manifest(a.package, a.version_code, a.version_name,
                            a.min_sdk, a.target_sdk, a.label, a.debug, a.icon_res)
    open(a.out, "wb").write(data)
    print(f"wrote {a.out} ({len(data)} bytes)")
