#!/usr/bin/env python3
"""apksign.py - APK Signature Scheme v2 signer + verifier (build tooling).

NOTA's application code is 100% AArch64 assembly. This script performs the
packaging-time job of `apksigner`: it produces the v2 APK Signing Block that
Android's PackageManager verifies at install time.

Wire format (frameworks/base/core/java/android/util/apk/ApkSignatureSchemeV2Verifier.java):

  APK: [entries][APK Signing Block][central directory][EOCD]
  EOCD.centralDirOffset -> start of the Signing Block

  APK Signing Block:
    uint64 size          == len(rest) ; rest = pairs + uint64 + magic
    pairs                = { uint64 len | uint32 id | value[len-4] }*
    uint64 size          (repeated)
    "APK Sig Block 42"

  v2 block value (id 0x7109871a):
    length-prefixed sequence of signers
      signer = length-prefixed (
          length-prefixed signed_data
        | length-prefixed signatures
        | length-prefixed public_key )
      signed_data = (
          length-prefixed digests        # sequence of entries
        | length-prefixed certificates   # sequence of DER certs
        | length-prefixed attributes )   # sequence of entries
        digest entry = uint32 sig_algo | length-prefixed digest
      signatures    = sequence of entries
        signature entry = uint32 sig_algo | length-prefixed signature

  Every "sequence" is prefixed with its byte length; every element inside a
  sequence is itself prefixed with its byte length.

  Content digest = SHA-256 over 3 chunks, each prefixed with 0xa5 + uint32 len:
    1) bytes before the Signing Block
    2) the central directory
    3) the EOCD with centralDirOffset replaced by the Signing Block offset
"""
import argparse
import hashlib
import struct
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SIG_BLOCK_MAGIC = b"APK Sig Block 42"
V2_BLOCK_ID = 0x7109871A
SIG_ALGO_RSA_PKCS1_SHA256 = 0x0103


def lp(b):
    return struct.pack("<I", len(b)) + b


def read_lp(buf, off):
    n = struct.unpack_from("<I", buf, off)[0]
    return buf[off + 4: off + 4 + n], off + 4 + n


def content_digest(data, block_start, cd_start, eocd_off):
    eocd = bytearray(data[eocd_off:])
    struct.pack_into("<I", eocd, 16, block_start)
    h = hashlib.sha256()
    for chunk in (data[:block_start], data[cd_start:eocd_off], bytes(eocd)):
        h.update(b"\xa5" + struct.pack("<I", len(chunk)))
        h.update(chunk)
    return h.digest()


def find_signing_block(data):
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise ValueError("no EOCD")
    cd_off = struct.unpack_from("<I", data, eocd + 16)[0]
    if cd_off < 32 or data[cd_off - 16:cd_off] != SIG_BLOCK_MAGIC:
        return eocd, cd_off, None
    size = struct.unpack_from("<Q", data, cd_off - 24)[0]
    start = cd_off - size - 8
    if struct.unpack_from("<Q", data, start)[0] != size:
        raise ValueError("signing block size fields disagree")
    return eocd, cd_off, start


def sign_apk(in_path, out_path, key_path, cert_path):
    key = serialization.load_pem_private_key(open(key_path, "rb").read(), None)
    cert = x509.load_pem_x509_certificate(open(cert_path, "rb").read())
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    pub_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)

    data = open(in_path, "rb").read()
    eocd_off, cd_off, existing = find_signing_block(data)
    if existing is not None:
        raise SystemExit("input is already signed; sign the unsigned APK")
    cd = data[cd_off:eocd_off]
    body = data[:cd_off]          # entries only
    eocd = bytearray(data[eocd_off:])

    digest = content_digest(body + cd + bytes(eocd), cd_off, cd_off, eocd_off)

    digest_entry = struct.pack("<I", SIG_ALGO_RSA_PKCS1_SHA256) + lp(digest)
    digests = lp(lp(digest_entry))
    signed_payload = digests + lp(cert_der) + lp(b"")
    signed_data = lp(signed_payload)

    signature = key.sign(signed_payload, padding.PKCS1v15(), hashes.SHA256())
    sig_entry = struct.pack("<I", SIG_ALGO_RSA_PKCS1_SHA256) + lp(signature)
    signatures = lp(lp(sig_entry))

    signer = lp(signed_data + signatures + lp(pub_der))
    value = lp(lp(signer))
    pair = struct.pack("<QI", len(value) + 4, V2_BLOCK_ID) + value

    size = len(pair) + 8 + len(SIG_BLOCK_MAGIC)
    block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + SIG_BLOCK_MAGIC

    struct.pack_into("<I", eocd, 16, cd_off + len(block))
    out = body + block + cd + bytes(eocd)
    open(out_path, "wb").write(out)

    ok, msg = verify_apk(out_path, cert_path)
    if not ok:
        raise SystemExit(f"self-verification failed: {msg}")
    return len(out)


def verify_apk(path, cert_path=None):
    data = open(path, "rb").read()
    try:
        eocd_off, cd_off, start = find_signing_block(data)
    except ValueError as exc:
        return False, str(exc)
    if start is None:
        return False, "no APK Signing Block"

    value = None
    off, end = start + 8, cd_off - 24
    while off < end:
        ln = struct.unpack_from("<Q", data, off)[0]
        pid = struct.unpack_from("<I", data, off + 8)[0]
        if pid == V2_BLOCK_ID:
            value = data[off + 12: off + 8 + ln]
        off += 8 + ln
    if value is None:
        return False, "no v2 signature block"

    try:
        signers, _ = read_lp(value, 0)
        signer_seq, _ = read_lp(signers, 0)
        signer, _ = read_lp(signer_seq, 0)
        signed_data, o = read_lp(signer, 0)
        signatures, o = read_lp(signer, o)
        pubkey, o = read_lp(signer, o)
        assert o == len(signer)

        digests, p = read_lp(signed_data, 0)
        certs, p = read_lp(signed_data, p)
        read_lp(signed_data, p)  # attributes

        # digests: sequence of (algo | lp digest) entries
        digest_entry, _ = read_lp(digests, 0)
        algo = struct.unpack_from("<I", digest_entry, 0)[0]
        dig_val, _ = read_lp(digest_entry, 4)

        # signatures: sequence of (algo | lp signature) entries
        sig_entry, _ = read_lp(signatures, 0)
        sig_algo = struct.unpack_from("<I", sig_entry, 0)[0]
        sig_val, _ = read_lp(sig_entry, 4)
    except Exception as exc:
        return False, f"malformed signature block: {exc}"

    if algo != sig_algo:
        return False, "digest/signature algorithm mismatch"
    if dig_val != content_digest(data, start, cd_off, eocd_off):
        return False, "content digest mismatch"

    pub = serialization.load_der_public_key(pubkey)
    try:
        pub.verify(sig_val, signed_data, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        return False, "signature verification failed"

    if cert_path:
        want = x509.load_pem_x509_certificate(open(cert_path, "rb").read())
        if want.public_bytes(serialization.Encoding.DER) not in certs:
            return False, "signing certificate not embedded"
    return True, "OK"


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sign")
    s.add_argument("--in", dest="inp", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--cert", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--apk", required=True)
    v.add_argument("--cert")
    a = p.parse_args()
    if a.cmd == "sign":
        n = sign_apk(a.inp, a.out, a.key, a.cert)
        print(f"signed {a.out} ({n} bytes, v2 RSA-2048/SHA-256)")
    else:
        ok, msg = verify_apk(a.apk, a.cert)
        print(f"{a.apk}: {msg}")
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
