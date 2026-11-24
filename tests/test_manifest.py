#!/usr/bin/env python3
"""test_manifest.py - checks on the compiled AndroidManifest.xml.

These do not need the assembly at all: they cover the AXML encoder in
tools/axml.py, which is the one part of the build that can produce an APK that
installs correctly and is still unusable.

That is not hypothetical. The activity was exported but carried no
intent-filter, so the app installed cleanly and then appeared in no launcher -
it could not be started by tapping an icon. Nothing in the assembly suites could
notice, because the fault was entirely in the manifest.

The tests parse the binary XML the encoder produces, not the Python that builds
it, so they fail if the encoder drops or mangles a tag.

Usage:  python3 tests/test_manifest.py [-v]
"""
import os
import struct
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import axml                                     # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ------------------------------------------------------------- AXML reader ---
# A minimal reader for the subset of binary XML the manifest uses. ResXMLTree
# stores every element name and string value in one string pool, and elements
# carry a list of (ns, name, raw, typed value) attributes. Everything below is
# parsing that, deliberately without trusting the encoder's own idea of what it
# wrote.
RES_STRING_POOL = 0x0001
RES_XML_START_ELEMENT = 0x0102
RES_XML_END_ELEMENT = 0x0103

TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
TYPE_INT_BOOLEAN = 0x12
TYPE_REFERENCE = 0x01

ANDROID_NS = "http://schemas.android.com/apk/res/android"


def _varint(data, pos):
    """AXML length prefix: 1 or 2 bytes, high bit signals continuation."""
    b = data[pos]
    if b & 0x80:
        return ((b & 0x7F) << 8) | data[pos + 1], pos + 2
    return b, pos + 1


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
            # A UTF-8 pool entry has *two* prefixes: characters, then bytes.
            # Reading only one shifts every string by the character count and
            # makes the whole manifest look like tag soup.
            _chars, p = _varint(data, p)
            nbytes, p = _varint(data, p)
            out.append(data[p:p + nbytes].decode("utf-8", "replace"))
        else:
            n, = struct.unpack_from("<H", data, p)
            p += 2
            out.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
    return out


def parse_axml(data):
    """-> (strings, [element]) where element = (tag, depth, {attr: value})."""
    strings = []
    elements = []
    off = 8
    while off < len(data):
        ctype, hsize, size = struct.unpack_from("<HHI", data, off)
        if size == 0:
            break
        if ctype == RES_STRING_POOL:
            strings = _string_pool(data, off)
        elif ctype in (RES_XML_START_ELEMENT, RES_XML_END_ELEMENT):
            depth, = struct.unpack_from("<i", data, off + 12)
            ns_i, name_i = struct.unpack_from("<II", data, off + 16)
            tag = strings[name_i] if name_i < len(strings) else ""
            attrs = {}
            if ctype == RES_XML_START_ELEMENT:
                # ResXMLTree_attrExt begins after the 16-byte node header.
                a_start, a_size, a_count = struct.unpack_from(
                    "<HHH", data, off + 16 + 8)
                for k in range(a_count):
                    a = off + 16 + a_start + k * a_size
                    a_ns, a_name, a_raw = struct.unpack_from("<III", data, a)
                    vtype = data[a + 15]
                    vdata, = struct.unpack_from("<I", data, a + 16)
                    if a_ns < len(strings) and strings[a_ns] == ANDROID_NS:
                        key = "android:" + strings[a_name]
                    else:
                        key = strings[a_name]
                    if vtype == TYPE_STRING:
                        val = strings[vdata] if vdata < len(strings) else ""
                    elif vtype == TYPE_INT_BOOLEAN:
                        val = vdata != 0
                    elif vtype == TYPE_INT_DEC:
                        val = vdata
                    elif vtype == TYPE_REFERENCE:
                        val = ("ref", vdata)
                    else:
                        val = ("type", vtype, vdata)
                    attrs[key] = val
            elements.append((tag, depth, attrs))
        off += size
    return strings, elements


# ------------------------------------------------------------------ fixture --
_cache = {}


def manifest(**kw):
    if "default" not in _cache:
        _cache["default"] = axml.compile_manifest(
            debug=True, icon_res=0x7F020000, **kw)
    return parse_axml(_cache["default"])


def find(elements, tag, depth=None):
    return [e for e in elements if e[0] == tag and (depth is None or e[1] == depth)]


def _manifest_elements():
    return manifest()


# -------------------------------------------------------------------- tests --
@test
def test_manifest_parses_at_all():
    strings, elements = manifest()
    assert strings, "empty string pool"
    assert elements, "no elements"
    assert elements[0][0] == "manifest"


@test
def test_launcher_intent_filter_puts_the_app_in_the_drawer():
    """The one that matters for installing without adb.

    An activity with no ACTION_MAIN/CATEGORY_LAUNCHER filter installs fine and
    is then invisible: no icon, and no way to start it by tapping. adb shell am
    start would still work, which is exactly why this went unnoticed.
    """
    _strings, elements = manifest()
    filters = find(elements, "intent-filter")
    assert filters, "no <intent-filter>: the app would have no launcher icon"

    actions, categories = [], []
    for tag, _depth, attrs in elements:
        if tag == "action":
            actions.append(attrs.get("android:name"))
        elif tag == "category":
            categories.append(attrs.get("android:name"))

    assert "android.intent.action.MAIN" in actions, actions
    assert "android.intent.category.LAUNCHER" in categories, categories


@test
def test_intent_filter_belongs_to_the_activity():
    """A filter on <application>, or after </activity>, does not register.

    The encoder does not write the optional depth field, so nesting is checked
    by document position: the filter must open after <activity> and before
    that activity closes.
    """
    _strings, elements = _manifest_elements()
    tags = [e[0] for e in elements]
    assert "activity" in tags and "intent-filter" in tags
    act = tags.index("activity")
    filt = tags.index("intent-filter")
    assert filt > act, "intent-filter appears before the activity"

    # no </activity> between the two, i.e. the filter is still inside it
    between = tags[act:filt]
    assert "activity" not in between[1:], "a new activity opened in between"


@test
def test_activity_is_exported_so_the_launcher_may_start_it():
    _strings, elements = manifest()
    acts = find(elements, "activity")
    assert acts, "no <activity>"
    attrs = acts[0][2]
    assert attrs.get("android:exported") is True, attrs.get("android:exported")


@test
def test_native_activity_and_lib_name_are_present():
    _strings, elements = manifest()
    acts = find(elements, "activity")
    assert acts[0][2].get("android:name") == "android.app.NativeActivity"

    meta = find(elements, "meta-data")
    values = {(m[2].get("android:name"), m[2].get("android:value")) for m in meta}
    assert ("android.app.lib_name", "nota") in values, values


@test
def test_extract_native_libs_is_off():
    """With extractNativeLibs="true" the loader unpacks the .so and our page
    alignment work in zipw.py stops mattering; with it false the library must
    be STORED and aligned, which the packaging path guarantees."""
    _strings, elements = manifest()
    apps = find(elements, "application")
    assert apps, "no <application>"
    assert apps[0][2].get("android:extractNativeLibs") is False


@test
def test_package_and_sdk_levels():
    _strings, elements = manifest()
    root = elements[0][2]
    assert root.get("package") == "com.revamptori.nota", root.get("package")
    sdk = find(elements, "uses-sdk")[0][2]
    assert sdk.get("android:minSdkVersion") == 26
    assert sdk.get("android:targetSdkVersion") == 34


@test
def test_the_app_requests_no_permissions():
    """A notes app that touches nothing needs nothing. A stray permission here
    is a privacy claim the code cannot back up."""
    _strings, elements = manifest()
    perms = find(elements, "uses-permission")
    assert not perms, [p[2] for p in perms]


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
    print("\n%d/%d manifest tests passed" % (passed, passed + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
