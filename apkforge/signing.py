"""APK signing without a JVM: JAR signature (v1), APK Signature Scheme v2, v3 and v4 (the .idsig sidecar), RSA PKCS#1 v1.5 + SHA-256.

Deterministic: PKCS#1 v1.5 has no random salt, so the same APK and key always give the same bytes (v4 included: its Merkle tree has no salt either).
Keys come from openssl (generation) or from a keystore (JKS/PKCS12 via keytool + openssl). verify_apk() below re-checks
every scheme without any external tool, and `apkforge verify` also asks Google's apksigner when it is installed.
"""
import base64
import datetime
import glob
import hashlib
import os
import re
import struct
import tempfile
import zipfile
import zlib

from . import util, zipw
from .util import ForgeError

SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")
ALG_RSA_PKCS1_SHA256 = 0x0103
V2_ID, V3_ID, PAD_ID = 0x7109871A, 0xF05368C0, 0x42726577
CHUNK = 1 << 20


# =============================================================================================== DER
def _dlen(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag, content):
    return bytes([tag]) + _dlen(len(content)) + content


def seq(*p):
    return tlv(0x30, b"".join(p))


def der_set(*p):
    return tlv(0x31, b"".join(p))


def der_int(n):
    b = n.to_bytes(max(1, (n.bit_length() + 8) // 8), "big")
    return tlv(0x02, b)


def der_oid(dotted):
    a = [int(x) for x in dotted.split(".")]
    out = bytes([a[0] * 40 + a[1]])
    for v in a[2:]:
        enc = [v & 0x7F]
        v >>= 7
        while v:
            enc.append(0x80 | (v & 0x7F))
            v >>= 7
        out += bytes(reversed(enc))
    return tlv(0x06, out)


NULL = b"\x05\x00"
OID_SHA256 = der_oid("2.16.840.1.101.3.4.2.1")
OID_RSA = der_oid("1.2.840.113549.1.1.1")
OID_DATA = der_oid("1.2.840.113549.1.7.1")
OID_SIGNED = der_oid("1.2.840.113549.1.7.2")


def read_tlv(b, off=0):
    """-> (tag, content_start, content_end)"""
    tag, ln, p = b[off], b[off + 1], off + 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(b[p:p + k], "big")
        p += k
    return tag, p, p + ln


def kids(b, start, end):
    out, o = [], start
    while o < end:
        t, s, e = read_tlv(b, o)
        out.append((t, s, e, o))
        o = e
    return out


def _uint(b, s, e):
    return int.from_bytes(b[s:e], "big")


# =============================================================================================== keys
def pem_blocks(text):
    return [(m.group(1), base64.b64decode(re.sub(r"\s+", "", m.group(2))))
            for m in re.finditer(r"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", text, re.S)]


def parse_cert(der):
    """-> dict(issuer=DER, serial=DER INTEGER, spki=DER, n, e)"""
    t, s, e = read_tlv(der, 0)
    tt, ts, te = read_tlv(der, kids(der, s, e)[0][3])          # tbsCertificate
    parts = kids(der, ts, te)
    i = 1 if parts[0][0] == 0xA0 else 0
    serial = der[parts[i][3]:parts[i][2]]
    issuer = der[parts[i + 2][3]:parts[i + 2][2]]
    spki = der[parts[i + 5][3]:parts[i + 5][2]]
    return {"issuer": issuer, "serial": serial, "spki": spki, **parse_spki(spki)}


def parse_spki(spki):
    t, s, e = read_tlv(spki, 0)
    k = kids(spki, s, e)
    if der_oid_value(spki, k[0]) != "1.2.840.113549.1.1.1":
        raise ForgeError("only RSA signing keys are supported by the built-in signer (got a different key type)")
    bs = k[1]
    inner = spki[bs[1] + 1:bs[2]]          # skip the unused-bits byte
    t, s, e = read_tlv(inner, 0)
    nn, ee = kids(inner, s, e)[:2]
    return {"n": _uint(inner, nn[1], nn[2]), "e": _uint(inner, ee[1], ee[2])}


def der_oid_value(b, seq_child):
    """First OID inside a SEQUENCE (algorithm identifier) as dotted text."""
    t, s, e = read_tlv(b, seq_child[3])
    t2, s2, e2 = read_tlv(b, s)
    raw = b[s2:e2]
    parts = [raw[0] // 40, raw[0] % 40]
    v = 0
    for x in raw[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))


def parse_rsa_private(der):
    t, s, e = read_tlv(der, 0)
    top = kids(der, s, e)
    if len(top) >= 3 and top[2][0] == 0x04 and top[1][0] == 0x30:      # PKCS#8 wrapper
        der = der[top[2][1]:top[2][2]]
        t, s, e = read_tlv(der, 0)
        top = kids(der, s, e)
    vals = [_uint(der, c[1], c[2]) for c in top[:9]]
    if len(vals) < 9:
        raise ForgeError("unsupported private key encoding (RSA expected)")
    return dict(zip(("version", "n", "e", "d", "p", "q", "dp", "dq", "qinv"), vals))


class Signer:
    def __init__(self, key_der, cert_ders):
        self.key = parse_rsa_private(key_der)
        self.certs = list(cert_ders)
        self.cert = parse_cert(self.certs[0])
        if self.cert["n"] != self.key["n"]:
            raise ForgeError("the certificate does not belong to the private key")
        self.bits = self.key["n"].bit_length()

    @property
    def fingerprint(self):
        return hashlib.sha256(self.certs[0]).hexdigest()

    def sign(self, data):
        """RSASSA-PKCS1-v1_5 with SHA-256 (CRT, with a fault check)."""
        k = (self.bits + 7) // 8
        t = SHA256_DIGESTINFO + hashlib.sha256(data).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        m = int.from_bytes(em, "big")
        K = self.key
        s1, s2 = pow(m % K["p"], K["dp"], K["p"]), pow(m % K["q"], K["dq"], K["q"])
        s = s2 + ((K["qinv"] * (s1 - s2)) % K["p"]) * K["q"]
        if pow(s, K["e"], K["n"]) != m:
            raise ForgeError("RSA self-check failed while signing")
        return s.to_bytes(k, "big")


def rsa_verify(n, e, data, sig):
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    t = SHA256_DIGESTINFO + hashlib.sha256(data).digest()
    return em == b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


# ------------------------------------------------------------------------------------ key sources
def _openssl():
    o = util.which("openssl")
    if not o:
        raise ForgeError("openssl is needed to create a signing key (apt-get install openssl), or pass an existing keystore")
    return o


def debug_signer(home=None):
    """A long-lived debug key kept in ~/.apkforge/keys (created on first use) so that debug builds update each other."""
    d = os.path.join(home or util.home_dir(), "keys")
    os.makedirs(d, exist_ok=True)
    kp, cp = os.path.join(d, "debug.key.pem"), os.path.join(d, "debug.cert.pem")
    if not (os.path.isfile(kp) and os.path.isfile(cp)):
        o = _openssl()
        util.run([o, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", kp], what="openssl genpkey")
        util.run([o, "req", "-new", "-x509", "-key", kp, "-sha256", "-days", "10000", "-subj",
                  "/CN=Android Debug/O=Android/C=US", "-out", cp], what="openssl req")
        os.chmod(kp, 0o600)
        util.info("created debug signing key " + kp)
    return signer_from_pem(open(kp).read(), open(cp).read())


def generate_keystore(path, alias, store_pass, dname, keytool, days=10000):
    """A release keystore: PKCS12, RSA 2048, valid about 27 years (Google Play wants a certificate that is valid past 2033). The password reaches keytool
    through the environment, never on its command line. The new file is read back with keystore_signer, so it is exactly what a release build will load.
    -> sha256 of the certificate"""
    if os.path.exists(path):
        raise ForgeError("%s already exists: keygen never overwrites a keystore (it may hold the only copy of an app's signing key); choose another --keystore" % path)
    _openssl()                      # keystore_signer needs it to read the file back: fail before a key exists, not after
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        util.run([keytool, "-genkeypair", "-keystore", path, "-storetype", "PKCS12", "-alias", alias, "-keyalg", "RSA", "-keysize", "2048",
                  "-validity", str(days), "-dname", dname, "-storepass:env", "APKFORGE_KEYGEN_PASS"],
                 env={"APKFORGE_KEYGEN_PASS": store_pass}, what="keytool -genkeypair")
        os.chmod(path, 0o600)
        return keystore_signer(path, alias, store_pass, store_pass).fingerprint
    except ForgeError:
        if os.path.exists(path):
            os.remove(path)         # a file this call made and could not use
        raise


def signer_from_pem(key_pem, cert_pem):
    keys = [d for l, d in pem_blocks(key_pem) if "PRIVATE KEY" in l and "ENCRYPTED" not in l]
    certs = [d for l, d in pem_blocks(cert_pem) if l == "CERTIFICATE"]
    if not keys or not certs:
        raise ForgeError("no unencrypted private key / certificate found in the PEM data")
    return Signer(keys[0], certs)


def keystore_signer(path, alias=None, store_pass=None, key_pass=None, keytool=None):
    """Load a signing key from a PKCS#12/JKS keystore or from a PEM file that holds the key and the certificate.  A JKS keystore is converted with keytool: `keytool`
    is the one to run (Toolchain.keytool(), which also finds the JDK apkforge downloaded), else the one on PATH."""
    if not os.path.isfile(path):
        raise ForgeError("keystore not found: " + path)
    head = open(path, "rb").read(64)
    if b"-----BEGIN" in head:
        txt = open(path).read()
        return signer_from_pem(txt, txt)
    sp, kp = store_pass or "android", key_pass or store_pass or "android"
    with tempfile.TemporaryDirectory() as td:
        p12 = path
        if head[:4] == b"\xfe\xed\xfe\xed" or head[:4] == b"\xce\xce\xce\xce":          # JKS / JCEKS -> PKCS12
            kt = keytool or util.which("keytool")
            if not kt:
                raise ForgeError("keytool is needed to read a JKS keystore (install a JRE/JDK, or run `apkforge doctor` to download a JDK)")
            p12 = os.path.join(td, "k.p12")
            # the passwords travel in the environment, never as arguments: an argument is readable in the process list and ends up in the build log
            cmd = [kt, "-importkeystore", "-srckeystore", path, "-srcstorepass:env", "APKFORGE_SRC_PASS", "-destkeystore", p12,
                   "-deststoretype", "PKCS12", "-deststorepass:env", "APKFORGE_DEST_PASS", "-destkeypass:env", "APKFORGE_DEST_PASS", "-noprompt"]
            if alias:
                cmd += ["-srcalias", alias]
            util.run(cmd, env={"APKFORGE_SRC_PASS": sp, "APKFORGE_DEST_PASS": kp}, what="keytool -importkeystore")
            sp = kp
        pem = os.path.join(td, "k.pem")
        util.run([_openssl(), "pkcs12", "-in", p12, "-nodes", "-passin", "env:APKFORGE_P12_PASS", "-out", pem],
                 env={"APKFORGE_P12_PASS": sp}, what="openssl pkcs12")
        txt = open(pem).read()
    return signer_from_pem(txt, txt)


# =============================================================================================== v1 (JAR)
def _wrap(line):
    b = line.encode("utf-8")
    out, first = [], True
    while True:
        lim = 70 if first else 69
        if len(b) <= lim:
            out.append((b if first else b" " + b) + b"\r\n")
            break
        cut = lim
        while cut > 0 and (b[cut] & 0xC0) == 0x80:      # never split a UTF-8 sequence
            cut -= 1
        out.append((b[:cut] if first else b" " + b[:cut]) + b"\r\n")
        b, first = b[cut:], False
    return b"".join(out)


def _b64(h):
    return base64.b64encode(h).decode()


def v1_files(entries, signer, with_v2=True, with_v3=False, created_by="apkforge"):
    """entries: [(name, bytes)] of everything except META-INF signature files. -> {name: bytes} for the 3 META-INF files."""
    skip = re.compile(r"^META-INF/(MANIFEST\.MF|[^/]+\.(SF|RSA|DSA|EC))$", re.I)
    sections = []
    mf = _wrap("Manifest-Version: 1.0") + _wrap("Built-By: " + created_by) + _wrap("Created-By: 1.0 (%s)" % created_by) + b"\r\n"
    for name, data in entries:
        if skip.match(name) or name.endswith("/"):
            continue
        sec = _wrap("Name: " + name) + _wrap("SHA-256-Digest: " + _b64(hashlib.sha256(data).digest())) + b"\r\n"
        sections.append(sec)
        mf += sec
    schemes = ", ".join(x for x, on in (("2", with_v2), ("3", with_v3)) if on)
    sf = _wrap("Signature-Version: 1.0") + _wrap("Created-By: 1.0 (%s)" % created_by)
    sf += _wrap("SHA-256-Digest-Manifest: " + _b64(hashlib.sha256(mf).digest()))
    if schemes:
        sf += _wrap("X-Android-APK-Signed: " + schemes)
    sf += b"\r\n"
    for sec in sections:
        name_part = sec[:sec.index(b"SHA-256-Digest: ")]      # the Name may have been wrapped over several lines
        sf += name_part + _wrap("SHA-256-Digest: " + _b64(hashlib.sha256(sec).digest())) + b"\r\n"
    sig = signer.sign(sf)
    cert0 = signer.certs[0]
    signer_info = seq(der_int(1), seq(signer.cert["issuer"], signer.cert["serial"]), seq(OID_SHA256, NULL),
                      seq(OID_RSA, NULL), tlv(0x04, sig))
    signed = seq(der_int(1), der_set(seq(OID_SHA256, NULL)), seq(OID_DATA),
                 tlv(0xA0, b"".join(signer.certs)), der_set(signer_info))
    p7 = seq(OID_SIGNED, tlv(0xA0, signed))
    return {"META-INF/MANIFEST.MF": mf, "META-INF/CERT.SF": sf, "META-INF/CERT.RSA": p7}


# =============================================================================================== v2 / v3
def _lp(b):
    return struct.pack("<I", len(b)) + bytes(b)


def content_digest(zip_without_block, cd_off, eocd_off):
    """APK Signature Scheme content digest (SHA-256, 1 MB chunks) over entries, central directory and EOCD."""
    mv = memoryview(zip_without_block)
    eocd = bytearray(mv[eocd_off:])
    struct.pack_into("<I", eocd, 16, cd_off)
    chunks = []
    for sec in (mv[:cd_off], mv[cd_off:eocd_off], memoryview(bytes(eocd))):
        for i in range(0, len(sec), CHUNK):
            c = sec[i:i + CHUNK]
            chunks.append(hashlib.sha256(b"\xa5" + struct.pack("<I", len(c)) + bytes(c)).digest())
    return hashlib.sha256(b"\x5a" + struct.pack("<I", len(chunks)) + b"".join(chunks)).digest()


def _digests_block(d):
    return _lp(struct.pack("<I", ALG_RSA_PKCS1_SHA256) + _lp(d))


def v2_pair(signer, digest):
    signed = _lp(_digests_block(digest)) + _lp(_lp(signer.certs[0]) + b"".join(_lp(c) for c in signer.certs[1:])) + _lp(b"")
    sigs = _lp(struct.pack("<I", ALG_RSA_PKCS1_SHA256) + _lp(signer.sign(signed)))
    one = _lp(signed) + _lp(sigs) + _lp(signer.cert["spki"])
    return V2_ID, _lp(_lp(one))


def v3_pair(signer, digest, min_sdk=28, max_sdk=0x7FFFFFFF):
    rng = struct.pack("<II", min_sdk, max_sdk)
    signed = _lp(_digests_block(digest)) + _lp(b"".join(_lp(c) for c in signer.certs)) + rng + _lp(b"")
    sigs = _lp(struct.pack("<I", ALG_RSA_PKCS1_SHA256) + _lp(signer.sign(signed)))
    one = _lp(signed) + rng + _lp(sigs) + _lp(signer.cert["spki"])
    return V3_ID, _lp(_lp(one))


def signing_block(pairs):
    """APK Signing Block, padded (verity padding) so that its total size is a multiple of 4096."""
    base = 8 + sum(12 + len(v) for _, v in pairs) + 8 + 16
    pairs = list(pairs)
    if base % 4096:
        pad = 4096 - base % 4096
        if pad < 12:
            pad += 4096
        pairs.append((PAD_ID, b"\0" * (pad - 12)))
    body = b"".join(struct.pack("<QI", len(v) + 4, i) + v for i, v in pairs)
    size = len(body) + 24
    return struct.pack("<Q", size) + body + struct.pack("<Q", size) + b"APK Sig Block 42"


def sign_zip(zb, signer, v1=True, v2=True, v3=True, v3_min_sdk=28):
    """zb: a ZipBuilder holding the unsigned APK entries. Returns the final, signed APK bytes."""
    if v1:
        for name, data in v1_files(list(zb.items()), signer, with_v2=v2, with_v3=v3).items():
            zb.add(name, data)
    if not (v2 or v3):
        return zb.finish()
    unsigned = zb.finish()
    cd_off, cd_size, eocd_off, _ = zipw.parse_sections(unsigned)
    digest = content_digest(unsigned, cd_off, eocd_off)
    pairs = []
    if v2:
        pairs.append(v2_pair(signer, digest))
    if v3:
        pairs.append(v3_pair(signer, digest, v3_min_sdk))
    return zb.finish(signing_block(pairs))


# =============================================================================================== v4 (.idsig)
# APK Signature Scheme v4 is a separate file, <apk>.idsig, next to the APK.  It holds an fs-verity style Merkle tree over every byte of the final APK and a
# signature over the root hash, so that `adb install` can stream the APK to a device and the device can check each 4 KiB block when it arrives (incremental
# install, Android 11+).  It needs a v2 or v3 signature inside the APK, by the same signer.  Layout (little endian; "bytes" = int32 length + data), checked
# byte for byte against what Google's apksig writes for the same APK and key (apksigner sign --v4-signing-enabled):
#   int32 version = 2
#   bytes hashing_info = int32 hash_algorithm (1 = SHA-256) | int8 log2_blocksize (12) | bytes salt (empty) | bytes raw_root_hash
#   bytes signing_info = bytes apk_digest | bytes certificate | bytes additional_data (empty) | bytes public_key | int32 signature_algorithm_id | bytes signature
#   bytes merkle_tree
# The signature covers: int32 size of this record | int64 apk size | int32 hash_algorithm | int8 log2_blocksize | bytes salt | bytes raw_root_hash |
# bytes apk_digest | bytes certificate | bytes additional_data.  apk_digest is the content digest the APK's own v3 (else v2) signature carries: it ties the two.
V4_VERSION, V4_SHA256, V4_LOG2_BLOCK, V4_BLOCK = 2, 1, 12, 4096
V4_SUFFIX = ".idsig"                     # the sidecar of <apk> is <apk>.idsig: that is the name adb and apksigner look for
V4_MAX_SIGNATURE = 8096                  # the largest signature (version, hashing info and signing info) Android's incremental file system accepts
V4_MAX_LEVELS = 16                       # a level holds 128 times fewer hashes than the one below it, so even a 2**63 byte file has 8 levels: more means a broken loop, which must fail, not run on


def merkle_tree(data):
    """The fs-verity Merkle tree of `data`: 4096-byte blocks (the last one zero-padded), SHA-256, no salt.  Every level is whole blocks of 32-byte hashes, the
    levels are stored top first (the root block first, the hashes of the data blocks last).  -> (tree, root hash); the root hash is the SHA-256 of the root block."""
    mv = memoryview(data)
    n = len(mv)
    if n == 0:
        raise ValueError("an empty file has no Merkle tree")
    full = n // V4_BLOCK
    hashes = [hashlib.sha256(mv[i * V4_BLOCK:(i + 1) * V4_BLOCK]).digest() for i in range(full)]
    if n % V4_BLOCK:
        hashes.append(hashlib.sha256(bytes(mv[full * V4_BLOCK:]).ljust(V4_BLOCK, b"\0")).digest())
    levels, level = [], b"".join(hashes)
    for _ in range(V4_MAX_LEVELS):
        level += b"\0" * (-len(level) % V4_BLOCK)
        levels.append(level)
        if len(level) == V4_BLOCK:
            break
        level = b"".join(hashlib.sha256(level[i:i + V4_BLOCK]).digest() for i in range(0, len(level), V4_BLOCK))
    else:
        raise ValueError("the Merkle tree of a %d byte file did not reach a single root block in %d levels" % (n, V4_MAX_LEVELS))
    return b"".join(reversed(levels)), hashlib.sha256(levels[-1]).digest()


def _v4_signed_data(apk_size, salt, root, apk_digest, cert, additional):
    body = struct.pack("<qiB", apk_size, V4_SHA256, V4_LOG2_BLOCK) + _lp(salt) + _lp(root) + _lp(apk_digest) + _lp(cert) + _lp(additional)
    return struct.pack("<i", 4 + len(body)) + body


def _signed_digest_and_cert(apk):
    """The content digest and the first certificate that the v3 (else v2) signature of this APK carries.  -> (digest, cert, "v3" | "v2"); ForgeError without both."""
    cd_off = zipw.parse_sections(apk)[0]
    _start, pairs, problems = _sig_block(apk, cd_off)
    if problems:
        raise ForgeError("the APK signing block is damaged: " + "; ".join(problems))
    which = "v3" if V3_ID in pairs else "v2" if V2_ID in pairs else None
    if not which:
        raise ForgeError("v4 needs a v2 or v3 signature inside the APK, and this one has neither")
    signer = _seq_items(_seq_items(pairs[V3_ID if which == "v3" else V2_ID])[0])[0]
    signed = _signer_parts(signer, which == "v3")[0]
    rec = _seq_items(_seq_items_one(signed, 0))[0]
    dlen = struct.unpack_from("<I", rec, 4)[0]
    return rec[8:8 + dlen], _seq_items(_seq_items_one(signed, 1))[0], which


def v4_signature(apk, signer):
    """The .idsig of a signed APK. apk = the FINAL bytes (v4 covers every byte of the file, so nothing may change after this); the APK must already carry a
    v2 or v3 signature made with `signer`.  -> the bytes of `<apk>.idsig`."""
    digest, cert, _which = _signed_digest_and_cert(apk)
    if cert != signer.certs[0]:
        raise ForgeError("v4: the APK is signed with another certificate than the key given for its v4 signature")
    tree, root = merkle_tree(apk)
    sig = signer.sign(_v4_signed_data(len(apk), b"", root, digest, cert, b""))
    hashing = struct.pack("<iB", V4_SHA256, V4_LOG2_BLOCK) + _lp(b"") + _lp(root)
    info = _lp(digest) + _lp(cert) + _lp(b"") + _lp(signer.cert["spki"]) + struct.pack("<i", ALG_RSA_PKCS1_SHA256) + _lp(sig)
    if 12 + len(hashing) + len(info) > V4_MAX_SIGNATURE:
        raise ForgeError("v4: the signature information is %d bytes and Android accepts at most %d (the certificate is unusually large)" % (12 + len(hashing) + len(info), V4_MAX_SIGNATURE))
    return struct.pack("<i", V4_VERSION) + _lp(hashing) + _lp(info) + _lp(tree)


class _Rd:
    """a bounds-checked little-endian reader: every overrun is a ValueError that says what was being read"""
    def __init__(self, b, what):
        self.b, self.p, self.what = b, 0, what

    def _take(self, n, field):
        if n < 0 or self.p + n > len(self.b):
            raise ValueError("%s: %s runs past the end (%d bytes left)" % (self.what, field, len(self.b) - self.p))
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def i32(self, field):
        return struct.unpack("<i", self._take(4, field))[0]

    def u8(self, field):
        return self._take(1, field)[0]

    def bytes(self, field):
        return bytes(self._take(self.i32(field + " (length)"), field))

    def done(self):
        if self.p != len(self.b):
            raise ValueError("%s: %d unexpected bytes after the last field" % (self.what, len(self.b) - self.p))


def parse_idsig(blob):
    """The bytes of an .idsig -> dict of its fields.  Raises ValueError, saying what is wrong, for anything that is not exactly the layout above."""
    r = _Rd(blob, "the .idsig")
    d = {"version": r.i32("version")}
    h = _Rd(r.bytes("hashing_info"), "hashing_info")
    d["hash_algorithm"], d["log2_blocksize"] = h.i32("hash_algorithm"), h.u8("log2_blocksize")
    d["salt"], d["root_hash"] = h.bytes("salt"), h.bytes("raw_root_hash")
    h.done()
    raw = r.bytes("signing_info")
    d["signature_size"] = 12 + len(raw) + len(h.b)
    s = _Rd(raw, "signing_info")
    d["apk_digest"], d["certificate"], d["additional_data"], d["public_key"] = (s.bytes(f) for f in ("apk_digest", "certificate", "additional_data", "public_key"))
    d["signature_algorithm_id"], d["signature"] = s.i32("signature_algorithm_id"), s.bytes("signature")
    s.done()
    d["tree"] = r.bytes("merkle_tree")
    r.done()
    return d


def check_v4(apk, blob):
    """Is `blob` (the bytes of `<apk>.idsig`) a v4 signature that belongs to `apk` (its final bytes) and verifies?  -> [problems]; [] = it does.
    Checks the file's structure, the Merkle tree against the APK, the signature, and that digest and certificate are those of the APK's own v2 / v3 signature."""
    if not apk:
        return ["the APK is empty"]
    try:
        d = parse_idsig(blob)
    except (ValueError, struct.error) as e:
        return [str(e)]
    if d["version"] != V4_VERSION:
        return ["version %d (this verifier reads version %d)" % (d["version"], V4_VERSION)]
    if (d["hash_algorithm"], d["log2_blocksize"]) != (V4_SHA256, V4_LOG2_BLOCK) or d["salt"]:
        return ["hashing_info is not SHA-256 over 4096-byte blocks without a salt, which is all this verifier reads"]
    out = []
    if d["signature_size"] > V4_MAX_SIGNATURE:
        out.append("the signature information is %d bytes and Android accepts at most %d" % (d["signature_size"], V4_MAX_SIGNATURE))
    if d["signature_algorithm_id"] != ALG_RSA_PKCS1_SHA256:
        out.append("signature algorithm 0x%04x is not supported by this verifier (only 0x%04x, RSA PKCS#1 v1.5 with SHA-256)" % (d["signature_algorithm_id"], ALG_RSA_PKCS1_SHA256))
    tree, root = merkle_tree(apk)
    if d["root_hash"] != root:
        out.append("the root hash is not that of this APK (the APK was changed after its .idsig was made, or the .idsig belongs to another APK)")
    elif d["tree"] != tree:
        out.append("the Merkle tree in the file differs from this APK's, although the root hash agrees")
    try:
        pub, cert = parse_spki(d["public_key"]), parse_cert(d["certificate"])
        if cert["spki"] != d["public_key"]:
            out.append("the public key is not the one of the certificate")
        elif d["signature_algorithm_id"] == ALG_RSA_PKCS1_SHA256 and not rsa_verify(
                pub["n"], pub["e"], _v4_signed_data(len(apk), d["salt"], d["root_hash"], d["apk_digest"], d["certificate"], d["additional_data"]), d["signature"]):
            out.append("the signature does not verify (it covers the APK's size, the root hash, the digest and the certificate)")
    except Exception as e:
        out.append("the certificate or public key cannot be read (%s: %s)" % (type(e).__name__, e))
    try:
        digest, cert_der, which = _signed_digest_and_cert(apk)
        if digest != d["apk_digest"]:
            out.append("apk_digest differs from the digest in the APK's %s signature (the APK was changed after signing, or the .idsig belongs to another build)" % which)
        if cert_der != d["certificate"]:
            out.append("the certificate differs from the signer of the APK's %s signature" % which)
    except ForgeError as e:
        out.append(str(e))
    except Exception as e:                  # a zip or a signing block that cannot be read at all: an answer, never a traceback
        out.append("the APK's v2 / v3 signature cannot be read (%s: %s)" % (type(e).__name__, e))
    return out


# =============================================================================================== self-verifier
def verify_apk(path):
    """Re-check v1/v2/v3 (and v4, when `<apk>.idsig` exists) without any external tool. -> dict(v1=bool|None, v2=..., v3=..., v4=..., problems=[...], cert_sha256=...)
    Fails closed: a file this cannot read, or one that carries no signature at all, comes back with a problem, never as an exception or as "no problems"."""
    try:
        return _verify_apk(path)
    except Exception as e:
        return {"v1": None, "v2": None, "v3": None, "v4": None, "problems": ["cannot read this file as an APK: %s: %s" % (type(e).__name__, e)], "cert_sha256": None}


def _sig_block(data, cd_off):
    """The APK Signing Block that ends where the central directory starts.  -> (start, {pair id: value}, [problems]); (None, {}, []) when there is none."""
    if data[cd_off - 16:cd_off] != b"APK Sig Block 42":
        return None, {}, []
    problems = []
    size = struct.unpack_from("<Q", data, cd_off - 24)[0]
    blk_start = cd_off - size - 8
    if struct.unpack_from("<Q", data, blk_start)[0] != size:
        problems.append("signing block size fields disagree")
    if (cd_off - blk_start) % 4096:
        problems.append("signing block is not a multiple of 4096 bytes")
    pairs, p = {}, blk_start + 8
    while p < cd_off - 24:
        ln, pid = struct.unpack_from("<QI", data, p)
        pairs[pid] = data[p + 12:p + 8 + ln]
        p += 8 + ln
    return blk_start, pairs, problems


def _verify_apk(path):
    import zipfile
    with open(path, "rb") as f:                  # (a bare open().read() leaked the handle until the garbage collector came by)
        data = f.read()
    rep = {"v1": None, "v2": None, "v3": None, "v4": None, "problems": [], "cert_sha256": None}
    cd_off, cd_size, eocd_off, n = zipw.parse_sections(data)
    # ---- v2 / v3
    blk_start, pairs, block_problems = _sig_block(data, cd_off)
    rep["problems"] += block_problems
    if blk_start is not None:
        stripped = data[:blk_start] + data[cd_off:]
        dig = content_digest(stripped, blk_start, eocd_off - (cd_off - blk_start))
        for pid, key, v3 in ((V2_ID, "v2", False), (V3_ID, "v3", True)):
            if pid not in pairs:
                continue
            try:
                rep[key] = _check_scheme(pairs[pid], dig, v3, rep)
            except Exception as e:      # malformed block
                rep[key] = False
                rep["problems"].append("%s: unreadable (%s)" % (key, e))
    # ---- v4: the sidecar <apk>.idsig, when there is one.  A sidecar that does not belong to this APK is wrong data beside it, so it is a problem, not a note.
    if os.path.isfile(path + V4_SUFFIX):
        with open(path + V4_SUFFIX, "rb") as f:
            bad = check_v4(data, f.read())
        rep["v4"] = not bad
        rep["problems"] += ["v4: " + b for b in bad]
    # ---- v1
    other_sf = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if "META-INF/MANIFEST.MF" in names and "META-INF/CERT.SF" in names:
                rep["v1"] = _check_v1(zf, names, rep)
            else:
                other_sf = [n for n in names if re.match(r"META-INF/[^/]+\.SF$", n)]
    except zipfile.BadZipFile as e:
        rep["problems"].append("zip: %s" % e)
    # ---- no signature of any scheme is not "no problems"
    if rep["v1"] is None and rep["v2"] is None and rep["v3"] is None and not rep["problems"]:
        if other_sf:
            rep["problems"].append("no signature this verifier can check: %s exists, but only META-INF/CERT.SF (with MANIFEST.MF) is read here and no v2 / v3 block was found" % other_sf[0])
        else:
            rep["problems"].append("not signed: no v1, v2 or v3 signature found")
    return rep


def _seq_items(b):
    out, p = [], 0
    while p < len(b):
        ln = struct.unpack_from("<I", b, p)[0]
        out.append(b[p + 4:p + 4 + ln])
        p += 4 + ln
    return out


def _signer_parts(signer, v3):
    """one signer record of a v2 / v3 value -> (signed data, signatures, public key)"""
    if not v3:
        signed, sigs, pub = _seq_items(signer)
        return signed, sigs, pub
    q = 0
    ln = struct.unpack_from("<I", signer, q)[0]; signed = signer[4:4 + ln]; q = 4 + ln
    q += 8                                                  # min/max sdk repeated outside
    ln = struct.unpack_from("<I", signer, q)[0]; sigs = signer[q + 4:q + 4 + ln]; q += 4 + ln
    ln = struct.unpack_from("<I", signer, q)[0]; pub = signer[q + 4:q + 4 + ln]
    return signed, sigs, pub


def _check_scheme(value, expect_digest, v3, rep):
    ok_all = True
    for signer in _seq_items(_seq_items(value)[0]):
        signed, sigs, pub = _signer_parts(signer, v3)
        digests = _seq_items(_seq_items_one(signed, 0))
        alg, dlen = struct.unpack_from("<II", digests[0], 0)
        got = digests[0][8:8 + dlen]
        if got != expect_digest:
            rep["problems"].append("content digest mismatch (the APK was modified after signing)")
            ok_all = False
        pk = parse_spki(pub)
        sig_algs = {struct.unpack_from("<I", sg, 0)[0] for sg in _seq_items(sigs)}
        dig_algs = {struct.unpack_from("<I", d, 0)[0] for d in digests}
        if not sig_algs:
            rep["problems"].append("the signer carries no signature")
            ok_all = False
        elif sig_algs != dig_algs:
            rep["problems"].append("the digests record lists signature algorithm(s) %s, the signatures record %s" % (
                ", ".join("0x%04x" % x for x in sorted(dig_algs)), ", ".join("0x%04x" % x for x in sorted(sig_algs))))
            ok_all = False
        for sg in _seq_items(sigs):
            a, sl = struct.unpack_from("<II", sg, 0)
            if a != ALG_RSA_PKCS1_SHA256:
                rep["problems"].append("signature algorithm 0x%04x is not supported by this verifier (only 0x%04x, RSA PKCS#1 v1.5 with SHA-256)" % (a, ALG_RSA_PKCS1_SHA256))
                ok_all = False
            elif not rsa_verify(pk["n"], pk["e"], signed, sg[8:8 + sl]):
                rep["problems"].append("signature does not verify")
                ok_all = False
        certs = _seq_items(_seq_items_one(signed, 1))
        rep["cert_sha256"] = hashlib.sha256(certs[0]).hexdigest()
    return ok_all


def _seq_items_one(b, index):
    """index-th length-prefixed element at the start of `b` (digests=0, certs=1, ...)."""
    p = 0
    for i in range(index + 1):
        ln = struct.unpack_from("<I", b, p)[0]
        if i == index:
            return b[p + 4:p + 4 + ln]
        p += 4 + ln


def _check_v1(zf, names, rep):
    mf = zf.read("META-INF/MANIFEST.MF")
    sf = zf.read("META-INF/CERT.SF")
    good = True
    secs = {}
    for blk in mf.split(b"\r\n\r\n")[1:]:
        if not blk.strip():
            continue
        txt = blk.replace(b"\r\n ", b"").decode("utf-8", "replace")
        m = re.match(r"Name: (.*)\r\nSHA-256-Digest: (.*)", txt)
        if m:
            secs[m.group(1)] = m.group(2).strip()
    for nm, dg in secs.items():
        if nm not in names or _b64(hashlib.sha256(zf.read(nm)).digest()) != dg:
            rep["problems"].append("v1: digest mismatch or missing entry: " + nm)
            good = False
    m = re.search(rb"SHA-256-Digest-Manifest: ([^\r]+)", sf)
    if not m or m.group(1).decode() != _b64(hashlib.sha256(mf).digest()):
        rep["problems"].append("v1: CERT.SF does not match MANIFEST.MF")
        good = False
    # stripping protection: CERT.SF lists the other schemes that signed this APK (X-Android-APK-Signed: 2, 3); each of them must still be there
    unwrapped = re.sub(rb"\r?\n ", b"", sf)
    ms = re.search(rb"(?im)^X-Android-APK-Signed:[ \t]*([^\r\n]*)", re.split(rb"\r?\n\r?\n", unwrapped, maxsplit=1)[0])
    if ms:
        for tok in ms.group(1).decode("ascii", "replace").split(","):
            tok = tok.strip()
            if tok in ("2", "3") and rep.get("v" + tok) is None:
                rep["problems"].append("v1: CERT.SF says the APK is also signed with v%s, but there is no v%s signature block (signature stripped?)" % (tok, tok))
                good = False
    rsa = next((n for n in names if re.match(r"META-INF/[^/]+\.RSA$", n)), None)
    if not rsa:
        rep["problems"].append("v1: no .RSA block")
        return False
    p7 = zf.read(rsa)
    t, s, e = read_tlv(p7, 0)
    ci = kids(p7, s, e)
    sd = read_tlv(p7, kids(p7, *read_tlv(p7, ci[1][3])[1:])[0][3])
    parts = kids(p7, sd[1], sd[2])
    certs = kids(p7, parts[3][1], parts[3][2])
    cert = parse_cert(p7[certs[0][3]:certs[0][2]])
    si = kids(p7, *read_tlv(p7, kids(p7, parts[-1][1], parts[-1][2])[0][3])[1:])
    sig = p7[si[-1][1]:si[-1][2]]
    if not rsa_verify(cert["n"], cert["e"], sf, sig):
        rep["problems"].append("v1: signature does not verify")
        good = False
    return good


# =============================================================================================== AAB (the JAR signature of an App Bundle)
# An .aab is a ZIP that Google Play wants signed like a JAR, with the UPLOAD key (what `jarsigner` does):
#   META-INF/MANIFEST.MF   every entry with the digest of its UNCOMPRESSED bytes
#   META-INF/<NAME>.SF     the digest of the whole manifest, of its main section and of every entry's manifest section
#   META-INF/<NAME>.RSA    a PKCS#7 signature over the .SF
# Measured against JDK 21's jarsigner (see tests/test_aab.py): the three files come first in the zip; the digest of an entry's section INCLUDES the blank line that ends
# it; the main-attributes digest likewise.  sign_aab() writes exactly that, and verify_aab() accepts what jarsigner writes (SHA-256/384/512, signed attributes).
# Entries are COPIED RAW (compressed bytes untouched): bundletool decided how each one is stored, signing does not recompress anything, and the same bundle plus the
# same key gives the same bytes (PKCS#1 v1.5 has no random salt, and no signing time is written).
JAR_SKIP = re.compile(r"^META-INF/(MANIFEST\.MF|[^/]+\.(SF|RSA|DSA|EC)|SIG-[^/]*)$", re.I)       # what jarsigner leaves out of the manifest
_DIGEST_HDR = re.compile(r"sha-?(1|224|256|384|512)-digest")
_WHOLE_HDR = re.compile(r"sha-?(1|224|256|384|512)-digest-manifest")
_MAIN_HDR = re.compile(r"sha-?(1|224|256|384|512)-digest-manifest-main-attributes")
HASH_OF_OID = {"1.3.14.3.2.26": "sha1", "2.16.840.1.101.3.4.2.4": "sha224", "2.16.840.1.101.3.4.2.1": "sha256",
               "2.16.840.1.101.3.4.2.2": "sha384", "2.16.840.1.101.3.4.2.3": "sha512"}
SIG_OIDS = {"1.2.840.113549.1.1.1": ("RSA", None), "1.2.840.113549.1.1.5": ("SHA1withRSA", "sha1"), "1.2.840.113549.1.1.14": ("SHA224withRSA", "sha224"),
            "1.2.840.113549.1.1.11": ("SHA256withRSA", "sha256"), "1.2.840.113549.1.1.12": ("SHA384withRSA", "sha384"),
            "1.2.840.113549.1.1.13": ("SHA512withRSA", "sha512"), "1.2.840.113549.1.1.10": ("RSASSA-PSS", None)}
DIGEST_INFO = {"sha1": bytes.fromhex("3021300906052b0e03021a05000414"), "sha224": bytes.fromhex("302d300d06096086480165030402040500041c"),
               "sha256": SHA256_DIGESTINFO, "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
               "sha512": bytes.fromhex("3051300d060960864801650304020305000440")}
OID_CONTENT_TYPE, OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.3", "1.2.840.113549.1.9.4"
OID_SHA256_RSA = der_oid("1.2.840.113549.1.1.11")
_DN_NAMES = {"2.5.4.3": "CN", "2.5.4.6": "C", "2.5.4.7": "L", "2.5.4.8": "ST", "2.5.4.9": "STREET", "2.5.4.10": "O", "2.5.4.11": "OU", "2.5.4.5": "SERIALNUMBER",
             "1.2.840.113549.1.9.1": "EMAILADDRESS", "0.9.2342.19200300.100.1.25": "DC", "0.9.2342.19200300.100.1.1": "UID"}


def rsa_verify_hash(n, e, hash_name, data, sig):
    """RSASSA-PKCS1-v1_5 verification with the hash the signature names (rsa_verify is the SHA-256 one the APK schemes use)."""
    k = (n.bit_length() + 7) // 8
    s = int.from_bytes(sig, "big")
    if len(sig) != k or s >= n or hash_name not in DIGEST_INFO:
        return False
    t = DIGEST_INFO[hash_name] + hashlib.new(hash_name, data).digest()
    if k < len(t) + 11:
        return False
    return pow(s, e, n).to_bytes(k, "big") == b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


# ------------------------------------------------------------------------------------ bounds-checked DER (for files from outside)
def _tlv(b, off, end):
    """-> (tag, content start, content end); ValueError when the element does not fit into b[off:end]"""
    if off + 2 > end:
        raise ValueError("DER: element runs past the end")
    tag, ln, p = b[off], b[off + 1], off + 2
    if tag & 0x1F == 0x1F:
        raise ValueError("DER: multi-byte tags are not used here")
    if ln & 0x80:
        k = ln & 0x7F
        if k == 0 or k > 4 or p + k > end:
            raise ValueError("DER: bad length")
        ln = int.from_bytes(b[p:p + k], "big")
        p += k
    if p + ln > end:
        raise ValueError("DER: element runs past the end")
    return tag, p, p + ln


def _children(b, s, e):
    """[(tag, content start, content end, element start)] of the elements inside b[s:e]"""
    out, o = [], s
    while o < e:
        t, cs, ce = _tlv(b, o, e)
        out.append((t, cs, ce, o))
        o = ce
    return out


def _oid_text(b, s, e):
    raw = b[s:e]
    if not raw:
        raise ValueError("DER: empty OBJECT IDENTIFIER")
    parts, v = [raw[0] // 40, raw[0] % 40], 0
    for x in raw[1:]:
        v = (v << 7) | (x & 0x7F)
        if not x & 0x80:
            parts.append(v)
            v = 0
    return ".".join(map(str, parts))


def _oid_of(b, child):
    """the OBJECT IDENTIFIER at `child` (a _children tuple), as dotted text; the first OID inside it when it is an AlgorithmIdentifier SEQUENCE"""
    if child[0] == 0x06:
        return _oid_text(b, child[1], child[2])
    inner = _children(b, child[1], child[2])
    if not inner or inner[0][0] != 0x06:
        raise ValueError("DER: an OBJECT IDENTIFIER was expected")
    return _oid_text(b, inner[0][1], inner[0][2])


def _dn_text(b, s, e):
    out = []
    for rdn in _children(b, s, e):
        for atv in _children(b, rdn[1], rdn[2]):
            kv = _children(b, atv[1], atv[2])
            raw = b[kv[1][1]:kv[1][2]]
            tag = kv[1][0]
            val = raw.decode("utf-16-be", "replace") if tag == 0x1E else raw.decode("utf-8" if tag == 0x0C else "latin-1", "replace")
            out.append("%s=%s" % (_DN_NAMES.get(_oid_of(b, kv[0]), _oid_of(b, kv[0])), val))
    return ", ".join(out)


def _der_time(tag, raw):
    t = raw.decode("ascii", "replace")
    if tag == 0x17 and len(t) >= 13:
        yy = int(t[:2])
        t = ("20" if yy < 50 else "19") + t
    elif tag != 0x18:
        raise ValueError("DER: not a time")
    return datetime.datetime(int(t[0:4]), int(t[4:6]), int(t[6:8]), int(t[8:10]), int(t[10:12]), int(t[12:14]), tzinfo=datetime.timezone.utc)


def cert_info(der):
    """What a person (and Google Play) asks of a certificate.  Unlike parse_cert it also reads keys that are not RSA.
    -> dict(subject, issuer, serial, not_before, not_after, sig_alg, key_alg, key_bits, self_signed, debug, sha256, issuer_der, serial_der, spki).  ValueError when unreadable."""
    t, s, e = _tlv(der, 0, len(der))
    top = _children(der, s, e)
    tbs = _children(der, top[0][1], top[0][2])
    i = 1 if tbs[0][0] == 0xA0 else 0
    serial, sigalg, issuer, validity, subject, spki = tbs[i], tbs[i + 1], tbs[i + 2], tbs[i + 3], tbs[i + 4], tbs[i + 5]
    nb, na = _children(der, validity[1], validity[2])[:2]
    sp = _children(der, spki[1], spki[2])
    key_oid = _oid_of(der, sp[0])
    bits = None
    if key_oid == "1.2.840.113549.1.1.1":
        try:
            bits = parse_spki(der[spki[3]:spki[2]])["n"].bit_length()
        except (ForgeError, IndexError, ValueError):
            bits = None
    subj, iss = _dn_text(der, subject[1], subject[2]), _dn_text(der, issuer[1], issuer[2])
    rdns = [x.strip() for x in subj.split(", ")]
    return {"subject": subj, "issuer": iss, "serial": _uint(der, serial[1], serial[2]), "not_before": _der_time(nb[0], der[nb[1]:nb[2]]), "not_after": _der_time(na[0], der[na[1]:na[2]]),
            "sig_alg": _oid_of(der, sigalg), "key_alg": {"1.2.840.113549.1.1.1": "RSA", "1.2.840.10045.2.1": "EC"}.get(key_oid, key_oid), "key_bits": bits,
            "self_signed": der[subject[3]:subject[2]] == der[issuer[3]:issuer[2]], "debug": "CN=Android Debug" in rdns and "O=Android" in rdns, "sha256": hashlib.sha256(der).hexdigest(),
            "issuer_der": der[issuer[3]:issuer[2]], "serial_der": der[serial[3]:serial[2]], "spki": der[spki[3]:spki[2]]}


# ------------------------------------------------------------------------------------ writing
def pkcs7_detached(content, signer, authenticated=True):
    """The .RSA file for `content` (the .SF): PKCS#7 SignedData, detached, SHA256withRSA.  With `authenticated` (what jarsigner writes) the signature covers the signed
    attributes contentType and messageDigest; no signing time, so the same input gives the same bytes."""
    sha = hashlib.sha256(content).digest()
    attrs = b"".join(sorted([seq(der_oid(OID_CONTENT_TYPE), der_set(OID_DATA)), seq(der_oid(OID_MESSAGE_DIGEST), der_set(tlv(0x04, sha)))]))      # DER SET OF: sorted
    sig = signer.sign(tlv(0x31, attrs) if authenticated else content)
    si = [der_int(1), seq(signer.cert["issuer"], signer.cert["serial"]), seq(OID_SHA256, NULL)]
    if authenticated:
        si.append(tlv(0xA0, attrs))
    si += [seq(OID_SHA256_RSA, NULL), tlv(0x04, sig)]
    signed = seq(der_int(1), der_set(seq(OID_SHA256, NULL)), seq(OID_DATA), tlv(0xA0, b"".join(signer.certs)), der_set(seq(*si)))
    return seq(OID_SIGNED, tlv(0xA0, signed))


def jar_signature_files(entries, signer, name="CERT", created_by="apkforge"):
    """entries: [(entry name, SHA-256 digest of its uncompressed bytes)] in zip order. -> {path: bytes} of META-INF/MANIFEST.MF, META-INF/<name>.SF and META-INF/<name>.RSA"""
    main = _wrap("Manifest-Version: 1.0") + _wrap("Created-By: 1.0 (%s)" % created_by) + b"\r\n"
    mf, parts = main, []
    for n, d in entries:
        if any(c in n for c in "\r\n\0"):
            raise ForgeError("the entry name %r cannot be written to a JAR manifest" % n)
        head = _wrap("Name: " + n)
        sec = head + _wrap("SHA-256-Digest: " + _b64(d)) + b"\r\n"
        parts.append((head, sec))
        mf += sec
    sf = (_wrap("Signature-Version: 1.0") + _wrap("Created-By: 1.0 (%s)" % created_by) + _wrap("SHA-256-Digest-Manifest: " + _b64(hashlib.sha256(mf).digest())) +
          _wrap("SHA-256-Digest-Manifest-Main-Attributes: " + _b64(hashlib.sha256(main).digest())) + b"\r\n")
    for head, sec in parts:
        sf += head + _wrap("SHA-256-Digest: " + _b64(hashlib.sha256(sec).digest())) + b"\r\n"
    return {"META-INF/MANIFEST.MF": mf, "META-INF/%s.SF" % name: sf, "META-INF/%s.RSA" % name: pkcs7_detached(sf, signer)}


def _dos(date_time):
    y, mo, d, h, mi, s = date_time
    return (h << 11) | (mi << 5) | (s // 2), ((max(y, 1980) - 1980) << 9) | (mo << 5) | d


def _name_bytes(info):
    return info.filename.encode("utf-8") if info.flag_bits & 0x800 else info.filename.encode("cp437")


def sign_aab(src, dst, signer, name="CERT", created_by="apkforge"):
    """Sign the App Bundle `src` into `dst` (the same path is fine: the new file is written beside it and moved over it).  A signature the bundle already carries is replaced.
    -> {"entries": n, "replaced": [old signature file names], "digest": "SHA-256", "file": "META-INF/<name>"}.  ForgeError for a bundle this writer cannot copy (zip64, encryption)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", name):
        raise ForgeError("the signature file name must be 1 to 8 letters, digits, _ or - (got %r)" % name)
    st = zipw.stamp_from_env()
    tmp = dst + ".signing"
    try:
        with zipfile.ZipFile(src) as zf, open(src, "rb") as raw, open(tmp, "wb") as out:
            infos, replaced, seen = [], [], set()
            for i in zf.infolist():
                n = i.filename
                if n in seen:
                    raise ForgeError("%s: the entry %s is there twice; a signature can cover only one of them" % (os.path.basename(src), n))
                seen.add(n)
                if n.startswith("/") or "\\" in n or ".." in n.split("/"):
                    raise ForgeError("%s: unsafe entry name %r" % (os.path.basename(src), n))
                if i.flag_bits & 1 or i.compress_type not in (0, 8) or max(i.file_size, i.compress_size, i.header_offset) > 0xFFFFFFFF or any(
                        k == 1 for k in _extra_ids(i.extra)):
                    raise ForgeError("%s: the entry %s is encrypted, uses an unsupported compression method or needs zip64; the built-in AAB signer cannot copy it" % (os.path.basename(src), n))
                if JAR_SKIP.match(n):
                    replaced.append(n)
                else:
                    infos.append(i)
            if len(infos) + 3 > 0xFFFF:
                raise ForgeError("%s: more than 65532 entries (zip64): not supported by the built-in AAB signer" % os.path.basename(src))
            digests = []
            for i in infos:                      # pass 1: digest every entry's uncompressed bytes (zipfile checks the CRC at the end of each)
                if i.filename.endswith("/"):
                    continue
                h = hashlib.sha256()
                try:
                    with zf.open(i) as f:
                        for chunk in iter(lambda: f.read(CHUNK), b""):
                            h.update(chunk)
                except (zipfile.BadZipFile, zlib.error, EOFError) as e:
                    raise ForgeError("%s: the entry %s is damaged (%s)" % (os.path.basename(src), i.filename, e))
                digests.append((i.filename, h.digest()))
            files = jar_signature_files(digests, signer, name, created_by)
            cd = []

            def put(nb, flags, method, dt, dd, crc, cs, us, made, attr):
                cd.append((nb, flags, method, dt, dd, crc, cs, us, made, attr, out.tell()))
                out.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20 if method == 8 else 10, flags, method, dt, dd, crc, cs, us, len(nb), 0) + nb)

            for fname, data in files.items():    # pass 2: the signature files first (as jarsigner does) ...
                co = zlib.compressobj(9, zlib.DEFLATED, -15)
                comp = co.compress(data) + co.flush()
                method, payload = (8, comp) if len(comp) < len(data) else (0, data)
                put(fname.encode("ascii"), 0, method, st[0], st[1], zlib.crc32(data) & 0xFFFFFFFF, len(payload), len(data), 20, 0)
                out.write(payload)
            for i in infos:                      # ... then every entry, its compressed bytes copied as they are, with a fresh header (no data descriptor)
                raw.seek(i.header_offset)
                hdr = raw.read(30)
                if len(hdr) != 30 or hdr[:4] != b"PK\x03\x04":
                    raise ForgeError("%s: the local header of %s is damaged" % (os.path.basename(src), i.filename))
                nlen, elen = struct.unpack_from("<HH", hdr, 26)
                raw.seek(i.header_offset + 30 + nlen + elen)
                dt, dd = _dos(i.date_time)
                put(_name_bytes(i), i.flag_bits & 0x0800, i.compress_type, dt, dd, i.CRC, i.compress_size, i.file_size, (i.create_system << 8) | 20, i.external_attr & 0xFFFFFFFF)
                left = i.compress_size
                while left:
                    chunk = raw.read(min(left, CHUNK))
                    if not chunk:
                        raise ForgeError("%s: the data of %s ends early" % (os.path.basename(src), i.filename))
                    out.write(chunk)
                    left -= len(chunk)
            cd_off = out.tell()
            for nb, flags, method, dt, dd, crc, cs, us, made, attr, off in cd:
                out.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, made, 20 if method == 8 else 10, flags, method, dt, dd, crc, cs, us, len(nb), 0, 0, 0, 0, attr, off) + nb)
            cd_size = out.tell() - cd_off
            if cd_off > 0xFFFFFFFF:
                raise ForgeError("the signed bundle would be over 4 GB (zip64): not supported by the built-in AAB signer")
            out.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(cd), len(cd), cd_size, cd_off, 0))
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return {"entries": len(infos), "replaced": replaced, "digest": "SHA-256", "file": "META-INF/" + name}


def _extra_ids(extra):
    out, p = [], 0
    while p + 4 <= len(extra):
        i, n = struct.unpack_from("<HH", extra, p)
        out.append(i)
        p += 4 + n
    return out


# ------------------------------------------------------------------------------------ verifying
_LINE = re.compile(rb"([^\r\n]*)(\r\n|\n|\r)")


def _jar_sections(data):
    """A META-INF/*.MF or *.SF -> [(the section's raw bytes INCLUDING its blank line, {lower-case header: value})]; the first one is the main section.  ValueError for a file that is not one."""
    out, cur, attrs, last, pos = [], bytearray(), {}, None, 0
    for m in _LINE.finditer(data):
        pos = m.end()
        line = m.group(1)
        cur += m.group(0)
        if line == b"":
            if attrs:
                out.append((bytes(cur), attrs))
            cur, attrs, last = bytearray(), {}, None
        elif line.startswith(b" "):
            if last is None:
                raise ValueError("a continuation line with nothing to continue")
            attrs[last] += line[1:].decode("utf-8", "replace")
        else:
            k, sep, v = line.partition(b": ")
            if not sep:
                raise ValueError("a line without \"name: value\": %r" % line[:40])
            last = k.decode("ascii", "replace").lower()
            attrs[last] = v.decode("utf-8", "replace")
    if pos != len(data):
        raise ValueError("the last line has no line terminator")
    if attrs:
        raise ValueError("the last section is not followed by a blank line")
    return out


def _b64d(v):
    return base64.b64decode(v.strip(), validate=True)


def _parse_pkcs7(p7):
    """The .RSA/.DSA/.EC file -> {"certs": [DER], "signers": [{issuer, serial (DER), digest_oid, attrs (DER content of the signed attributes, or None), attr_list, sig_oid, sig}]}"""
    t, s, e = _tlv(p7, 0, len(p7))
    top = _children(p7, s, e)
    if t != 0x30 or len(top) < 2 or _oid_of(p7, top[0]) != "1.2.840.113549.1.7.2" or top[1][0] != 0xA0:
        raise ValueError("not a PKCS#7 SignedData")
    inner = _children(p7, top[1][1], top[1][2])
    sd = _children(p7, inner[0][1], inner[0][2])
    k, certs = 3, []
    if sd[k][0] == 0xA0:
        certs = [p7[c[3]:c[2]] for c in _children(p7, sd[k][1], sd[k][2])]
        k += 1
    if k < len(sd) and sd[k][0] == 0xA1:
        k += 1
    if k >= len(sd) or sd[k][0] != 0x31:
        raise ValueError("the SignedData has no signerInfos")
    signers = []
    for si in _children(p7, sd[k][1], sd[k][2]):
        parts = _children(p7, si[1], si[2])
        if parts[1][0] != 0x30:
            raise ValueError("a signer named by a key identifier is not supported")
        ias = _children(p7, parts[1][1], parts[1][2])
        j, attrs, attr_list = 3, None, []
        if parts[j][0] == 0xA0:
            attrs = p7[parts[j][1]:parts[j][2]]
            for a in _children(p7, parts[j][1], parts[j][2]):
                ac = _children(p7, a[1], a[2])
                vals = _children(p7, ac[1][1], ac[1][2])
                attr_list.append((_oid_of(p7, ac[0]), [(v[0], p7[v[1]:v[2]]) for v in vals]))
            j += 1
        signers.append({"issuer": p7[ias[0][3]:ias[0][2]], "serial": p7[ias[1][3]:ias[1][2]], "digest_oid": _oid_of(p7, parts[2]), "attrs": attrs, "attr_list": attr_list,
                        "sig_oid": _oid_of(p7, parts[j]), "sig": p7[parts[j + 1][1]:parts[j + 1][2]]})
    return {"certs": certs, "signers": signers}


def _check_p7(p7, sf):
    """-> (signer description or None, [problems], [warnings]) for one .RSA block over the .SF bytes `sf`"""
    pr, wr = [], []
    info = _parse_pkcs7(p7)
    if not info["signers"]:
        return None, ["the signature block holds no signer"], wr
    if len(info["signers"]) > 1:
        wr.append("the signature block holds %d signers; Google Play expects one" % len(info["signers"]))
    si = info["signers"][0]
    cert = None
    for c in info["certs"]:
        try:
            ci = cert_info(c)
        except (ValueError, IndexError):
            continue
        if ci["issuer_der"] == si["issuer"] and ci["serial_der"] == si["serial"]:
            cert = ci
            break
    if cert is None:
        return None, ["the signer's certificate (issuer and serial number) is not in the signature block"], wr
    dig = HASH_OF_OID.get(si["digest_oid"])
    sig_name, sig_hash = SIG_OIDS.get(si["sig_oid"], (si["sig_oid"], None))
    if dig is None:
        pr.append("unsupported digest algorithm %s" % si["digest_oid"])
    h = sig_hash or dig
    signed = sf
    if si["attrs"] is not None:
        signed = tlv(0x31, si["attrs"])
        attrs = dict(si["attr_list"])
        md = attrs.get(OID_MESSAGE_DIGEST)
        if not md or md[0][0] != 0x04:
            pr.append("the signed attributes carry no messageDigest")
        elif dig and md[0][1] != hashlib.new(dig, sf).digest():
            pr.append("the signature block was made for another .SF file (its messageDigest differs from the .SF's digest)")
        ct = attrs.get(OID_CONTENT_TYPE)
        if ct and (ct[0][0] != 0x06 or ct[0][1] != OID_DATA[2:]):
            pr.append("the signed attributes name a content type other than data")
    verified = None
    if dig is None or h is None:
        if not pr:
            pr.append("the signature algorithm %s is not supported by this verifier (RSA with SHA-1/224/256/384/512 is)" % sig_name)
    elif cert["key_alg"] != "RSA" or si["sig_oid"] == "1.2.840.113549.1.1.10":
        wr.append("the signing key or algorithm (%s %s) is not RSA PKCS#1 v1.5: this verifier cannot check that signature (jarsigner -verify can)" % (cert["key_alg"], sig_name))
    else:
        pk = parse_spki(cert["spki"])
        verified = rsa_verify_hash(pk["n"], pk["e"], h, signed, si["sig"])
        if not verified:
            pr.append("the signature over the .SF file does not verify with the signer's certificate")
    if dig == "sha1" or h == "sha1":
        wr.append("the signature uses SHA-1, which JDK 17+ and Google Play treat as weak (sign with SHA-256)")
    if cert["key_bits"] and cert["key_bits"] < 2048:
        wr.append("the signing key is RSA %d bits (2048 or more is expected)" % cert["key_bits"])
    return dict(cert, digest=(dig or "?").upper().replace("SHA", "SHA-"), signature_algorithm=sig_name, verified=verified), pr, wr


def verify_aab(path):
    """Check the JAR signature of an App Bundle without any external tool, the way jarsigner does but stricter: every file in the bundle must be listed in the manifest
    and match its digest, every manifest section must be covered by the .SF, and the PKCS#7 signature must verify.  Fails closed: a file this cannot read comes back
    with a problem, never as an exception or as "no problems".
    -> {"signed": bool, "problems": [...], "warnings": [...], "signers": [{file, subject, issuer, not_before, not_after, sha256, key_alg, key_bits, digest, signature_algorithm, self_signed, debug, verified}], "entries": n}"""
    try:
        return _verify_aab(path)
    except Exception as e:
        return {"signed": False, "problems": ["cannot read this file as an App Bundle: %s: %s" % (type(e).__name__, e)], "warnings": [], "signers": [], "entries": 0}


def _verify_aab(path):
    rep = {"signed": False, "problems": [], "warnings": [], "signers": [], "entries": 0}
    pr, wr = rep["problems"], rep["warnings"]
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        rep["entries"] = sum(1 for n in names if not n.endswith("/"))
        for n in sorted({n for n in names if names.count(n) > 1}):
            pr.append("zip: the entry %s is there twice (the signature can cover only one of them)" % n)
        for n in names:
            if n.startswith("/") or "\\" in n or ".." in n.split("/"):
                pr.append("zip: unsafe entry name %r" % n)
        upper = {n.upper(): n for n in names}
        if "META-INF/MANIFEST.MF" not in names:
            pr.append("not signed: there is no META-INF/MANIFEST.MF")
            return rep
        sigs = sorted(n for n in names if re.fullmatch(r"META-INF/[^/]+\.SF", n, re.I))
        if not sigs:
            pr.append("not signed: the manifest is there but no signature file (META-INF/*.SF)")
            return rep
        rep["signed"] = True
        mf = zf.read("META-INF/MANIFEST.MF")
        try:
            msecs = _jar_sections(mf)
        except ValueError as e:
            pr.append("META-INF/MANIFEST.MF: %s" % e)
            return rep
        main_raw = msecs[0][0] if msecs else b""
        want = {}
        for raw, attrs in msecs[1:]:
            nm = attrs.get("name")
            if nm is None:
                pr.append("META-INF/MANIFEST.MF: a section without a Name")
                continue
            digs = {}
            for k, v in attrs.items():
                m = _DIGEST_HDR.fullmatch(k)
                if m:
                    try:
                        digs["sha" + m.group(1)] = _b64d(v)
                    except ValueError:
                        pr.append("META-INF/MANIFEST.MF: the digest of %s is not base64" % nm)
            if not digs:
                pr.append("META-INF/MANIFEST.MF: the section of %s has no digest, so the entry is not signed" % nm)
            elif set(digs) == {"sha1"}:
                wr.append("%s is digested with SHA-1 only" % nm)
            want[nm] = digs
        by_name = {i.filename: i for i in infos}
        bad = []
        for nm, digs in want.items():
            i = by_name.get(nm)
            if i is None or nm.endswith("/"):
                pr.append("META-INF/MANIFEST.MF lists %s, which is not in the bundle" % nm)
                continue
            hs = {a: hashlib.new(a) for a in digs}
            try:
                with zf.open(i) as f:
                    for chunk in iter(lambda: f.read(CHUNK), b""):
                        for h in hs.values():
                            h.update(chunk)
            except (zipfile.BadZipFile, zlib.error, EOFError) as e:
                pr.append("zip: %s is damaged (%s)" % (nm, e))
                continue
            if any(hs[a].digest() != d for a, d in digs.items()):
                bad.append(nm)
        if bad:
            pr.append("%d entr%s changed after signing (digest mismatch): %s" % (len(bad), "y was" if len(bad) == 1 else "ies were", ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")))
        loose = [n for n in names if not n.endswith("/") and not JAR_SKIP.match(n) and n not in want]
        if loose:
            pr.append("%d file%s not listed in META-INF/MANIFEST.MF, so the signature does not cover %s: %s" % (
                len(loose), " is" if len(loose) == 1 else "s are", "it" if len(loose) == 1 else "them", ", ".join(loose[:5]) + (" ..." if len(loose) > 5 else "")))
        for sfn in sigs:
            base = sfn[:-3]
            blk = next((upper[base.upper() + ext] for ext in (".RSA", ".DSA", ".EC") if base.upper() + ext in upper), None)
            sf = zf.read(sfn)
            try:
                ssecs = _jar_sections(sf)
            except ValueError as e:
                pr.append("%s: %s" % (sfn, e))
                continue
            sattrs = ssecs[0][1] if ssecs else {}
            if "signature-version" not in sattrs:
                pr.append("%s: no Signature-Version header" % sfn)
            whole = [(m.group(1), v) for k, v in sattrs.items() for m in [_WHOLE_HDR.fullmatch(k)] if m]
            matched = False
            for alg, v in whole:
                try:
                    good = hashlib.new("sha" + alg, mf).digest() == _b64d(v)
                except ValueError:
                    good = False
                matched = matched or good
            if whole and not matched:
                pr.append("%s: its digest of META-INF/MANIFEST.MF does not match (the manifest was changed after signing)" % sfn)
            for k, v in sattrs.items():
                m = _MAIN_HDR.fullmatch(k)
                if m and hashlib.new("sha" + m.group(1), main_raw).digest() != _b64d(v):
                    pr.append("%s: its digest of the manifest's main section does not match" % sfn)
            if not matched:                      # the way jarsigner falls back: every manifest section must carry its digest in the .SF
                sections = {a["name"]: a for _r, a in ssecs[1:] if "name" in a}
                for raw, attrs in msecs[1:]:
                    sec = sections.get(attrs.get("name"))
                    good = bool(sec) and any(hashlib.new("sha" + m.group(1), raw).digest() == _b64d(v) for k, v in sec.items() for m in [_DIGEST_HDR.fullmatch(k)] if m)
                    if not good:
                        pr.append("%s: the entry %s is not covered by the signature file" % (sfn, attrs.get("name")))
                        break
            if blk is None:
                pr.append("%s has no signature block (%s.RSA, .DSA or .EC)" % (sfn, base))
                continue
            try:
                sg, p, w = _check_p7(zf.read(blk), sf)
            except (ValueError, IndexError, ForgeError) as e:
                pr.append("%s: cannot be read as a PKCS#7 signature (%s)" % (blk, e))
                continue
            pr += ["%s: %s" % (blk, x) for x in p]
            wr += w
            if sg:
                sg["file"] = base
                sg["not_before"], sg["not_after"] = sg["not_before"].isoformat(), sg["not_after"].isoformat()
                for k in ("issuer_der", "serial_der", "spki"):
                    sg.pop(k, None)
                rep["signers"].append(sg)
        if len(rep["signers"]) > 1:
            wr.append("the bundle carries %d signatures; Google Play expects exactly one" % len(rep["signers"]))
    return rep
