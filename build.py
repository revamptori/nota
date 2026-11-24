#!/usr/bin/env python3
"""build.py - build NOTA into a signed APK with no Gradle and no NDK.

Pipeline:
    src/*.S + app/glue.c  ->  lib/arm64-v8a/libnota.so
    AndroidManifest.xml   ->  compiled AXML           (tools/axml.py)
    resources             ->  resources.arsc + icon  (tools/arsc.py)
    everything            ->  base.apk, aligned      (tools/zipw.py)
    base.apk              ->  signed APK, v2 scheme  (tools/apksign.py)

The only external requirement is a cross-compiler; `zig cc -target
aarch64-linux-android` is used by default because it needs no NDK install.

Usage:
    python3 build.py [--cc "..."] [--out out/nota.apk]
                     [--key KEY.pem --cert CERT.pem]
    python3 build.py --unsigned --out out/nota-unsigned.apk

With no --key, a throwaway key is generated into out/ so the artifact is
installable; a real release must pass its own.
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))

import apksign          # noqa: E402
import arsc             # noqa: E402
import axml             # noqa: E402
import zipw             # noqa: E402

SOURCES = ["libc", "utf8", "sys", "edit", "store", "font", "tex", "text",
           "ui", "screen", "app", "main", "callbacks", "lifecycle"]
CFLAGS = ["-fPIC", "-Isrc"]
SHARED = ["-shared", "-nostdlib", "-fPIC", "-Wl,-soname,libnota.so"]
LIBS = ["-Lbuild/stubs", "-landroid", "-lEGL", "-lGLESv2", "-llog"]

DEFAULT_CC = "zig cc -target aarch64-linux-android"


def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        sys.stderr.write(" ".join(cmd) + "\n" + r.stdout + r.stderr)
        raise SystemExit("build failed: %s" % cmd[0])
    return r.stdout


def cc_list(spec):
    return spec.split()


def need_cc(cc):
    if not shutil.which(cc[0]):
        raise SystemExit(
            "compiler not found: %s\n"
            "Pass --cc, or install zig (which needs no NDK)." % cc[0])


def build_native(cc, out_dir, verbose):
    """Compile every source and link libnota.so."""
    obj = os.path.join("build", "obj")
    shutil.rmtree(obj, ignore_errors=True)
    os.makedirs(obj, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    # link-time stubs for the NDK libraries, so no NDK is required
    stubs = os.path.join("build", "stubs")
    before = set(os.listdir(stubs)) if os.path.isdir(stubs) else set()
    run([sys.executable, "tools/mkstubs.py", "--out", stubs, "--cc", " ".join(cc)])
    if verbose:
        print("stubs:", ", ".join(sorted(os.listdir(stubs))))

    objs = []
    for name in SOURCES:
        src = os.path.join("src", name + ".S")
        o = os.path.join(obj, name + ".o")
        run(cc + ["-c"] + CFLAGS + ["-o", o, src])
        objs.append(o)
    o = os.path.join(obj, "glue.o")
    run(cc + ["-c", "-Isrc", "-o", o, os.path.join("app", "glue.c")])
    objs.append(o)

    lib = os.path.join(out_dir, "libnota.so")
    run(cc + SHARED + ["-o", lib] + objs + LIBS)
    if verbose:
        print("linked: %s (%d bytes)" % (lib, os.path.getsize(lib)))
    return lib


def build_apk(lib, out_path, release):
    """Assemble the zip and write it."""
    package = "com.revamptori.nota"
    icon_res = 0x7F020000
    with open(lib, "rb") as fh:
        libdata = fh.read()

    manifest = axml.compile_manifest(
        package=package, version_code=1, version_name="1.0.0",
        min_sdk=26, target_sdk=34, label="NOTA", debug=not release,
        icon_res=icon_res, lib_name="nota")

    rsc = arsc.build_arsc(package_name=package, icon_res_id=icon_res,
                          label="NOTA")
    icon = arsc.make_icon_png()

    # extractNativeLibs=false means the loader mmaps the library straight out
    # of the APK. That requires it to be STORED (a mmap cannot start mid
    # deflate stream) *and* page-aligned inside the zip, which is why it is
    # last: everything before it is small and its alignment padding is then a
    # one-time cost rather than shifting later entries.
    entries = [
        zipw.Entry("AndroidManifest.xml", manifest, align=4),
        zipw.Entry("resources.arsc", rsc, align=4),
        zipw.Entry("res/mipmap/ic_launcher.png", icon, align=4),
        zipw.Entry("lib/arm64-v8a/libnota.so", libdata,
                   method=zipw.STORED, align=4096),
    ]
    data = zipw.build(entries)
    with open(out_path, "wb") as fh:
        fh.write(data)
    return len(data)


def ensure_key(key_path, cert_path, cert_cn="NOTA"):
    """Create a throwaway signing key if the caller did not supply one."""
    if os.path.exists(key_path) and os.path.exists(cert_path):
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cert_cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=10000))
            .sign(key, hashes.SHA256()))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))


# Everything the library may legitimately import. A -shared link does not fail
# on an undefined symbol, so a call to a helper that was never written ships as
# a .so with an unresolved import and dies at dlopen on the device. Listing the
# allowed prefixes turns that into a build failure.
UNDEFINED_OK = ("gl", "egl", "AInput", "AKey", "AMotion", "ANative",
                "ALooper", "AAsset", "pthread_", "__")


def check_undefined(lib):
    from elftools.elf.elffile import ELFFile
    undefined = set()
    with open(lib, "rb") as fh:
        for sec in ELFFile(fh).iter_sections():
            if sec.name != ".symtab":
                continue
            for sym in sec.iter_symbols():
                if sym["st_shndx"] == "SHN_UNDEF" and sym.name:
                    undefined.add(sym.name)
    bad = sorted(n for n in undefined
                 if not n.startswith(UNDEFINED_OK))
    if bad:
        raise SystemExit(
            "undefined symbols in %s: %s\n"
            "Either the helper was never written, or a source file is missing "
            "from build_native's list." % (lib, ", ".join(bad)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cc", default=os.environ.get("NOTA_CC", DEFAULT_CC))
    ap.add_argument("--out", default=os.path.join("out", "nota.apk"))
    ap.add_argument("--unsigned", action="store_true")
    ap.add_argument("--release", action="store_true",
                    help="non-debuggable manifest")
    ap.add_argument("--key", default=None)
    ap.add_argument("--cert", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    cc = cc_list(a.cc)
    need_cc(cc)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)

    if a.verbose:
        print("compiler:", " ".join(cc))
        print("building native library...")
    lib = build_native(cc, os.path.join("build", "apk", "lib"),
                       a.verbose)
    check_undefined(lib)

    unsigned = a.out
    if not a.unsigned:
        unsigned = os.path.join(os.path.dirname(a.out),
                                os.path.basename(a.out).replace(
                                    ".apk", "-unsigned.apk"))
    if a.verbose:
        print("packaging...")
    size = build_apk(lib, unsigned, a.release)
    if a.verbose:
        print("packaged: %s (%d bytes)" % (unsigned, size))

    if a.unsigned:
        print("%s  (%d bytes, unsigned)" % (unsigned, size))
        return 0

    key = a.key or os.path.join("out", "debug.pem")
    cert = a.cert or os.path.join("out", "debug-cert.pem")
    if not (a.key and a.cert):
        ensure_key(key, cert)
    apksign.sign_apk(unsigned, a.out, key, cert)
    os.unlink(unsigned)

    ok, msg = apksign.verify_apk(a.out, cert if a.cert is None else a.cert)
    print("%s  (%d bytes)" % (a.out, os.path.getsize(a.out)))
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
