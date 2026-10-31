"""Tests for apkforge's Android App Bundle support: the protobuf readers, the bundle config, Google Play's findings, the bundle signer and its verifier, the universal APK,
the upload-key section, and (when the toolchain is installed) the real thing: a demo app built into a bundle, read back by jarsigner and bundletool, tried out as a universal APK.

Run it from the folder that holds apkforge/ :      python3 -m unittest discover -s tests -v
The tests that need aapt2, D8, android.jar, bundletool and a JDK skip themselves when those are not installed.  Nothing here uses the network by itself.
"""
import contextlib
import io
import json
import os
import shutil
import stat
import struct
import sys
import tempfile
import types
import unittest
import warnings
import zipfile
from unittest import mock

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
from apkforge import aab, cli, project, resopt, signing, templates, util, zipw     # noqa: E402
from apkforge import build as B                                                    # noqa: E402
from apkforge import manifest as M                                                 # noqa: E402
from apkforge import toolchain as T                                                # noqa: E402
from apkforge.util import ForgeError                                               # noqa: E402

ANDROID = "http://schemas.android.com/apk/res/android"


# ----------------------------------------------------------------------------------------------------------- hand-made protobuf
def varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def vi(num, n):
    return varint(num << 3) + varint(n)


def ld(num, payload):
    payload = payload.encode() if isinstance(payload, str) else payload
    return varint(num << 3 | 2) + varint(len(payload)) + payload


def attr(name, value, uri=""):
    return ld(4, (ld(1, uri) if uri else b"") + ld(2, name) + ld(3, value))


def element(name, attrs=(), children=()):
    return ld(3, name) + b"".join(attrs) + b"".join(ld(5, ld(1, c)) for c in children)


def manifest_blob(debuggable=False, package="com.acme.t"):
    a = lambda n, v: attr(n, v, ANDROID)                                           # noqa: E731
    main = element("activity", [a("name", "com.acme.t.Main"), a("exported", "true")], [
        element("intent-filter", [], [element("action", [a("name", "android.intent.action.MAIN")]), element("category", [a("name", "android.intent.category.LAUNCHER")])])])
    app = element("application", [a("debuggable", "true")] if debuggable else [], [main])
    root = element("manifest", [attr("package", package), a("versionCode", "7"), a("versionName", "1.2")],
                   [element("uses-sdk", [a("minSdkVersion", "24"), a("targetSdkVersion", "35")]), app])
    return ld(1, root)


def config_blob(version="1.18.3"):
    collapsed = vi(1, 1) + ld(2, ld(1, "string") + ld(2, "app_name")) + vi(3, 1)
    return ld(1, ld(2, version)) + ld(2, ld(5, vi(1, 2) + ld(2, collapsed))) + ld(3, ld(1, "**.hbc"))


def bundle_zip(path, manifest=None, config=None, extra=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BundleConfig.pb", config_blob() if config is None else config)
        z.writestr("base/manifest/AndroidManifest.xml", manifest_blob() if manifest is None else manifest)
        z.writestr("base/resources.pb", b"\x0a\x00")
        z.writestr("base/dex/classes.dex", b"dex\n035\0" + b"x" * 200)
        for n, d in (extra or {}).items():
            z.writestr(n, d)
    return path


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def rd(path):
    with open(path, "rb") as f:
        return f.read()


def run_cli(*argv, env=None):
    """one `apkforge` command in this process -> (exit status, stdout, stderr).  util._CONSOLE is reset for the call: log_to_stderr() (build --json, verify, test-bundle --json) keeps
    the stderr of the call that made it, and a real process never runs a second command"""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}), mock.patch.object(util, "_CONSOLE", None), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


NO_ORACLES = lambda tc, path: aab.no_oracles()                                      # noqa: E731


# ============================================================================================================== the protobuf reader
class WireTests(unittest.TestCase):
    def test_every_wire_type(self):
        data = vi(1, 300) + ld(2, "hi") + varint(3 << 3 | 5) + b"\1\2\3\4" + varint(4 << 3 | 1) + b"\0" * 8
        self.assertEqual(list(aab.fields(data)), [(1, 0, 300), (2, 2, b"hi"), (3, 5, b"\1\2\3\4"), (4, 1, b"\0" * 8)])

    def test_the_last_value_of_a_singular_field_wins(self):
        self.assertEqual(aab._s(aab.msg(ld(1, "a") + ld(1, "b")), 1), "b")

    def test_refusals(self):
        for bad in (b"\x80", b"\x0a\x05ab", b"\x0b", b"\x00\x00", b"\x08" + b"\xff" * 11, b"\x0d\x01\x02"):
            with self.assertRaises(aab.ProtoError, msg=repr(bad)):
                list(aab.fields(bad))


class DecoderTests(unittest.TestCase):
    def test_config(self):
        c = aab.decode_config(config_blob())
        self.assertEqual(c, {"bundletool_version": "1.18.3", "type": "REGULAR", "sparse_encoding": "VARIANT_FOR_SDK_32", "collapse_resource_names": True,
                             "deduplicate_resource_entries": True, "no_collapse_resources": ["string/app_name"], "uncompressed_glob": ["**.hbc"]})
        j = {"bundletool": {"version": "1.18.3"}, "compression": {"uncompressedGlob": ["**.hbc"]},
             "optimizations": {"resourceOptimizations": {"sparseEncoding": "VARIANT_FOR_SDK_32", "collapsedResourceNames": {
                 "collapseResourceNames": True, "deduplicateResourceEntries": True, "noCollapseResources": [{"type": "string", "name": "app_name"}]}}}}
        self.assertEqual(aab.config_diffs(c, aab.config_from_json(j), aab.CONFIG_KEYS + ("bundletool_version", "type")), [])
        j["compression"]["uncompressedGlob"] = []
        self.assertEqual(len(aab.config_diffs(c, aab.config_from_json(j))), 1)

    def test_empty_and_unknown_config(self):
        c = aab.decode_config(b"")
        self.assertEqual((c["sparse_encoding"], c["type"], c["collapse_resource_names"], c["uncompressed_glob"]), ("UNSPECIFIED", "REGULAR", False, []))
        self.assertEqual(aab.decode_config(ld(2, ld(5, vi(1, 7))))["sparse_encoding"], "UNKNOWN(7)")
        self.assertEqual(aab.describe_decoded(c), "bundletool defaults")
        self.assertIn("collapsed", aab.describe_decoded(aab.decode_config(config_blob())))

    def test_manifest(self):
        root = aab.decode_manifest(manifest_blob(debuggable=True))
        self.assertEqual(aab.manifest_summary(root), {"package": "com.acme.t", "versionCode": 7, "versionName": "1.2", "minSdk": 24, "targetSdk": 35, "debuggable": True})
        self.assertEqual(M.launchers(root), [{"name": "com.acme.t.Main", "category": "LAUNCHER", "alias_of": None}])
        self.assertFalse(aab.manifest_summary(aab.decode_manifest(manifest_blob()))["debuggable"])

    def test_manifest_refusals(self):
        with self.assertRaises(aab.ProtoError):
            aab.decode_manifest(b"")
        with self.assertRaises(aab.ProtoError):
            aab.decode_manifest(ld(1, ld(2, "x")))                                  # an element without a name
        node = element("leaf")
        for _ in range(aab.MAX_DEPTH + 6):
            node = element("e", [], [node])
        with self.assertRaises(aab.ProtoError):
            aab.decode_manifest(ld(1, node))


class ConfigMappingTests(unittest.TestCase):
    def test_nothing_to_say(self):
        self.assertEqual(aab.bundle_config(None, None), (None, []))
        self.assertEqual(aab.bundle_config(resopt.Options(shorten=False, sparse=False), None), (None, []))
        self.assertEqual(aab.describe_config(None), "none")

    def test_the_defaults_of_optimize_resources_true(self):
        cfg, notes = aab.bundle_config(resopt.Options(), None)
        self.assertEqual(cfg, {"optimizations": {"resourceOptimizations": {"sparseEncoding": "VARIANT_FOR_SDK_32"}}})
        self.assertEqual(len(notes), 1)
        self.assertIn("shortenPaths", notes[0])
        self.assertIn("sparse", aab.describe_config(cfg))

    def test_collapse_keep_dedupe_and_react(self):
        cfg, notes = aab.bundle_config(resopt.Options(shorten=False, sparse=False, collapse=True, dedupe=True, keep=["string/a", "layout/b"]), "hbc")
        self.assertEqual(cfg["optimizations"]["resourceOptimizations"]["collapsedResourceNames"],
                         {"collapseResourceNames": True, "deduplicateResourceEntries": True, "noCollapseResources": [{"type": "string", "name": "a"}, {"type": "layout", "name": "b"}]})
        self.assertEqual(cfg["compression"], {"uncompressedGlob": ["**.hbc"]})
        self.assertEqual(notes, [])
        cfg, _ = aab.bundle_config(resopt.Options(shorten=False, sparse=False, collapse=True, dedupe=False, keep=[]), None)
        self.assertEqual(cfg["optimizations"]["resourceOptimizations"]["collapsedResourceNames"], {"collapseResourceNames": True})

    def test_dedupe_alone_and_the_reserved_value(self):
        cfg, _ = aab.bundle_config(resopt.Options(shorten=False, sparse=True, collapse=False, dedupe=True), None)
        self.assertNotIn("deduplicateResourceEntries", json.dumps(cfg))            # aapt2 dedupes only together with collapse, and so does this
        self.assertNotIn("ENFORCED", json.dumps(cfg))                              # reserved in config.proto: bundletool rejects the whole file

    def test_react_only(self):
        self.assertEqual(aab.bundle_config(None, "hbc"), ({"compression": {"uncompressedGlob": ["**.hbc"]}}, []))

    def test_keep_names_must_be_resources(self):
        table = types.SimpleNamespace(packages=[])
        with mock.patch.object(resopt, "table_names", return_value={"string/app_name", "layout/main"}):
            resopt.check_keep(["string/app_name"], table)
            with self.assertRaises(ForgeError) as cm:
                resopt.check_keep(["string/app_nam"], table)
        self.assertIn("did you mean string/app_name", str(cm.exception))


class PlayFindingsTests(unittest.TestCase):
    @staticmethod
    def sig(**kw):
        s = {"subject": "CN=Android Debug", "debug": False, "not_after": "2050-01-01T00:00:00+00:00"}
        s.update(kw)
        return {"signed": True, "signers": [s]}

    def test_debug_certificate_and_debuggable_are_errors(self):
        f = aab.play_findings({"debuggable": True, "package": "com.acme.t"}, self.sig(debug=True))
        self.assertEqual((len(f["errors"]), f["warnings"]), (2, []))

    def test_clean(self):
        self.assertEqual(aab.play_findings({"debuggable": False, "package": "com.acme.t"}, self.sig()), {"errors": [], "warnings": []})

    def test_certificate_validity_ends_after_22_october_2033(self):
        for until, warned in (("2030-01-01T00:00:00+00:00", True), ("2033-10-22T00:00:00+00:00", True), ("2033-10-23T00:00:00+00:00", False)):
            f = aab.play_findings({"package": "com.acme.t"}, self.sig(not_after=until))
            self.assertEqual((bool(f["warnings"]), f["errors"]), (warned, []), until)

    def test_unsigned(self):
        unsigned = {"signed": False, "signers": []}
        self.assertEqual(len(aab.play_findings({"package": "a.b"}, unsigned)["errors"]), 1)
        self.assertEqual(aab.play_findings({"package": "a.b"}, unsigned, unsigned_ok=True)["errors"], [])

    def test_com_example(self):
        for pkg, warned in (("com.example", True), ("com.example.app", True), ("com.examples", False)):
            self.assertEqual(bool(aab.play_findings({"package": pkg}, self.sig())["warnings"]), warned, pkg)


class ReadBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_reads_a_bundle(self):
        b, root = aab.read_bundle(bundle_zip(os.path.join(self.tmp, "a.aab")))
        self.assertEqual(b["problems"], [])
        self.assertEqual((b["package"], b["versionCode"], b["minSdk"], b["targetSdk"], b["modules"]), ("com.acme.t", 7, 24, 35, ["base"]))
        self.assertEqual(b["config"]["sparse_encoding"], "VARIANT_FOR_SDK_32")
        self.assertEqual(b["launchers"][0]["name"], "com.acme.t.Main")
        self.assertIsNotNone(root)

    def test_a_bad_file_is_a_problem_never_an_exception(self):
        junk = os.path.join(self.tmp, "junk.aab")
        with open(junk, "wb") as f:
            f.write(b"not a zip")
        only_config = os.path.join(self.tmp, "c.aab")
        with zipfile.ZipFile(only_config, "w") as z:
            z.writestr("BundleConfig.pb", config_blob())
        for path, text in ((junk, "cannot read this file"), (only_config, "no base module"), (bundle_zip(os.path.join(self.tmp, "m.aab"), manifest=b"\xff\xff\xff"), "proto manifest"),
                           (bundle_zip(os.path.join(self.tmp, "k.aab"), config=b"\xff\xff"), "BundleConfig.pb cannot be read")):
            b, root = aab.read_bundle(path)
            self.assertTrue(any(text in p for p in b["problems"]), (text, b["problems"]))


# ============================================================================================================== the signer
class SignerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not util.which("openssl"):
            raise unittest.SkipTest("openssl is needed for the signing key")
        cls.home = tempfile.mkdtemp()
        with quiet():
            cls.signer = signing.debug_signer(cls.home)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = bundle_zip(os.path.join(self.tmp, "in.aab"))

    def sign(self, name="out.aab"):
        out = os.path.join(self.tmp, name)
        return out, signing.sign_aab(self.src, out, self.signer)

    def rewrite(self, src, dst, change=None, drop=(), add=None):
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
            for i in zin.infolist():
                if i.filename not in drop:
                    zout.writestr(i, (change or {}).get(i.filename, zin.read(i.filename)))
            for n, d in (add or {}).items():
                zout.writestr(n, d)
        return dst

    def test_round_trip(self):
        out, res = self.sign()
        self.assertEqual((res["digest"], res["entries"], res["replaced"], res["file"]), ("SHA-256", 4, [], "META-INF/CERT"))
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.namelist()[:3], ["META-INF/MANIFEST.MF", "META-INF/CERT.SF", "META-INF/CERT.RSA"])       # first, as jarsigner writes them
        rep = signing.verify_aab(out)
        self.assertTrue(rep["signed"])
        self.assertEqual((rep["problems"], rep["warnings"], len(rep["signers"])), ([], [], 1))
        s = rep["signers"][0]
        self.assertEqual((s["verified"], s["debug"], s["digest"], s["key_bits"], s["sha256"]), (True, True, "SHA-256", 2048, self.signer.fingerprint))

    def test_deterministic_and_re_signing_in_place(self):
        a, _ = self.sign("a.aab")
        b, _ = self.sign("b.aab")
        self.assertEqual(rd(a), rd(b))
        c = os.path.join(self.tmp, "c.aab")
        shutil.copy(a, c)
        res = signing.sign_aab(c, c, self.signer)
        self.assertEqual(res["replaced"], ["META-INF/MANIFEST.MF", "META-INF/CERT.SF", "META-INF/CERT.RSA"])
        self.assertEqual(rd(c), rd(a))

    def test_a_changed_entry(self):
        out, _ = self.sign()
        rep = signing.verify_aab(self.rewrite(out, os.path.join(self.tmp, "bad.aab"), change={"base/resources.pb": b"\x0a\x01x"}))
        self.assertTrue(rep["signed"])
        self.assertTrue(any("changed after signing" in p for p in rep["problems"]), rep["problems"])

    def test_a_file_the_signature_does_not_cover(self):
        out, _ = self.sign()
        rep = signing.verify_aab(self.rewrite(out, os.path.join(self.tmp, "bad.aab"), add={"base/assets/x.txt": b"x"}))
        self.assertTrue(any("not listed in META-INF/MANIFEST.MF" in p for p in rep["problems"]), rep["problems"])

    def test_no_signature_file_unsigned_and_garbage(self):
        out, _ = self.sign()
        rep = signing.verify_aab(self.rewrite(out, os.path.join(self.tmp, "nosf.aab"), drop={"META-INF/CERT.SF"}))
        self.assertFalse(rep["signed"])
        self.assertTrue(any("no signature file" in p for p in rep["problems"]))
        rep = signing.verify_aab(self.src)
        self.assertEqual((rep["signed"], any("not signed" in p for p in rep["problems"])), (False, True))
        junk = os.path.join(self.tmp, "junk")
        with open(junk, "wb") as f:
            f.write(b"not a zip at all")
        rep = signing.verify_aab(junk)
        self.assertEqual((rep["signed"], rep["signers"]), (False, []))
        self.assertTrue(rep["problems"][0].startswith("cannot read this file"))

    def test_a_duplicate_entry(self):
        dup = os.path.join(self.tmp, "dup.aab")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(dup, "w") as z:
                z.writestr("base/a.txt", b"1")
                z.writestr("base/a.txt", b"2")
        with self.assertRaises(ForgeError) as cm:
            signing.sign_aab(dup, os.path.join(self.tmp, "o.aab"), self.signer)
        self.assertIn("there twice", str(cm.exception))
        self.assertTrue(any("there twice" in p for p in signing.verify_aab(dup)["problems"]))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "o.aab.signing")))             # nothing is left behind

    def test_verify_bundle_without_external_readers(self):
        out, _ = self.sign()
        with mock.patch.object(aab, "run_bundle_oracles", NO_ORACLES):
            rep = B.verify_bundle(T.Toolchain(fetch_missing=False), out)
            self.assertEqual(rep["problems"], [])
            tags = {c["claim"]: B.claim_tag(c) for c in rep["claims"]}
            self.assertEqual(tags["bundle-signature"], "internal only; jarsigner not found (it comes with a JDK)")
            self.assertTrue(tags["bundle-structure"].startswith("not checked"))
            self.assertEqual(set(rep["oracles"].values()), {"missing"})
            rep = B.verify_bundle(T.Toolchain(fetch_missing=False), self.src)                     # unsigned: a problem ...
            self.assertTrue(any("not signed" in p for p in rep["problems"]))
            rep = B.verify_bundle(T.Toolchain(fetch_missing=False), self.src, unsigned_ok=True)    # ... unless it is unsigned on purpose
            self.assertEqual(rep["problems"], [])
            self.assertEqual(rep["claims"][0]["status"], "unchecked")


class UniversalRepackTests(unittest.TestCase):
    @staticmethod
    def offset(blob, info):
        n, e = struct.unpack("<HH", blob[info.header_offset + 26:info.header_offset + 30])
        return info.header_offset + 30 + n + e

    def test_layout(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("classes.dex", b"d" * 4000, zipfile.ZIP_DEFLATED)
            z.writestr("AndroidManifest.xml", b"m" * 100, zipfile.ZIP_DEFLATED)
            z.writestr("resources.arsc", b"r" * 333, zipfile.ZIP_STORED)
            z.writestr("lib/arm64-v8a/libx.so", b"s" * 1000, zipfile.ZIP_STORED)
            z.writestr("res/raw/a.bin", b"b" * 77, zipfile.ZIP_STORED)
            z.writestr("assets/b.txt", b"hello hello hello " * 50, zipfile.ZIP_DEFLATED)
            z.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\r\n\r\n")
            z.writestr("META-INF/services/foo", b"x")
        blob = aab.repack_universal(buf.getvalue()).finish()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names, infos = z.namelist(), {i.filename: i for i in z.infolist()}
        self.assertEqual(names[0], "AndroidManifest.xml")
        self.assertNotIn("META-INF/MANIFEST.MF", names)
        self.assertIn("META-INF/services/foo", names)
        self.assertEqual(infos["resources.arsc"].compress_type, zipfile.ZIP_STORED)
        self.assertEqual(infos["classes.dex"].compress_type, zipfile.ZIP_DEFLATED)
        self.assertEqual(self.offset(blob, infos["resources.arsc"]) % 4, 0)
        self.assertEqual(self.offset(blob, infos["res/raw/a.bin"]) % 4, 0)
        self.assertEqual(self.offset(blob, infos["lib/arm64-v8a/libx.so"]) % zipw.PAGE_16K, 0)

    def test_a_duplicate_name_is_refused(self):
        buf = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("a", b"1")
                z.writestr("a", b"2")
        with self.assertRaises(ValueError):
            aab.repack_universal(buf.getvalue())


# ============================================================================================================== the configuration
class UploadSigningConfigTests(unittest.TestCase):
    @staticmethod
    def module(**extra):
        cfg = {"applicationId": "com.acme.t"}
        cfg.update(extra)
        return project.Module(":app", tempfile.gettempdir(), cfg)

    def test_accepted(self):
        self.module()
        self.module(uploadSigning=None)
        self.module(uploadSigning={"keystore": "upload.p12", "alias": "upload", "storePasswordEnv": "X_PASS", "keyPasswordEnv": "Y_PASS"})

    def test_refused(self):
        for bad in ("upload.p12", True, [], {}, {"keystore": ""}, {"keystore": 5}, {"keystore": "k", "alias": 1}, {"keystore": "k", "storePassword": ""},
                    {"keystore": "k", "storePsswordEnv": "X"}):
            with self.assertRaises(ForgeError, msg=repr(bad)):
                self.module(uploadSigning=bad)


class SignerSelectionTests(unittest.TestCase):
    @staticmethod
    def pick(release, cfg):
        fake = types.SimpleNamespace(release=release, keystore_signer_for=lambda m, section: "signer:" + section)
        with mock.patch.object(signing, "debug_signer", return_value="signer:debug"):
            return B.Builder.aab_signer(fake, types.SimpleNamespace(cfg=cfg))

    def test_which_key_signs_the_bundle(self):
        both = {"signing": {"keystore": "a"}, "uploadSigning": {"keystore": "b"}}
        self.assertEqual(self.pick(True, both), ("signer:uploadSigning", "uploadSigning"))
        self.assertEqual(self.pick(True, {"signing": {"keystore": "a"}}), ("signer:signing", "signing"))
        self.assertEqual(self.pick(True, {}), ("signer:debug", "debug"))
        self.assertEqual(self.pick(False, both), ("signer:debug", "debug"))              # a debug build: the debug key, like the APK

    def test_the_keystore_of_a_section(self):
        if not (util.which("keytool") and util.which("openssl")):
            self.skipTest("keytool and openssl are needed")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        ks = os.path.join(tmp, "up.p12")
        signing.generate_keystore(ks, "upload", "pw-123456", "CN=Acme Upload, O=Acme, C=US", util.which("keytool"))
        fake = types.SimpleNamespace(release=True, tc=types.SimpleNamespace(keytool=lambda: util.which("keytool")))     # a Builder has a toolchain; the keytool is its answer
        m = types.SimpleNamespace(dir=tmp, cfg={"uploadSigning": {"keystore": "up.p12", "alias": "upload", "storePasswordEnv": "T_UP_PASS"}})
        with mock.patch.dict(os.environ, {"T_UP_PASS": "pw-123456"}):
            s = B.Builder.keystore_signer_for(fake, m, "uploadSigning")
        self.assertEqual(s.fingerprint, signing.keystore_signer(ks, "upload", "pw-123456", "pw-123456").fingerprint)
        with mock.patch.dict(os.environ, {}), self.assertRaises(ForgeError) as cm:
            os.environ.pop("T_UP_PASS", None)
            B.Builder.keystore_signer_for(fake, m, "uploadSigning")
        self.assertIn("uploadSigning.storePasswordEnv", str(cm.exception))


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not util.which("openssl"):
            raise unittest.SkipTest("openssl is needed for the signing key")
        cls.home = tempfile.mkdtemp()
        with quiet():
            cls.signer = signing.debug_signer(cls.home)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.unsigned = bundle_zip(os.path.join(self.tmp, "u.aab"))
        self.signed = os.path.join(self.tmp, "s.aab")
        signing.sign_aab(self.unsigned, self.signed, self.signer)

    def test_no_sign_aab_needs_aab(self):
        rc, _, err = run_cli("build", self.tmp, "--no-sign-aab")
        self.assertEqual(rc, 1)
        self.assertIn("--no-sign-aab", err)

    def test_verify_a_bundle(self):
        with mock.patch.object(aab, "run_bundle_oracles", NO_ORACLES):
            rc, out, _ = run_cli("verify", self.signed)
            self.assertEqual(rc, 0)
            rep = json.loads(out)
            self.assertTrue(rep["signed"])
            self.assertEqual((rep["bundle"]["package"], rep["problems"]), ("com.acme.t", []))
            self.assertTrue(rep["play"]["errors"])                                  # the debug certificate: logged, not a failure ...
            self.assertEqual(run_cli("verify", self.signed, "--play")[0], 1)         # ... unless --play asks for it
            self.assertEqual(run_cli("verify", self.unsigned)[0], 1)
            self.assertEqual(run_cli("verify", self.unsigned, "--allow-unsigned")[0], 0)
            rc, _, err = run_cli("verify", os.path.join(self.tmp, "missing.aab"))
            self.assertEqual(rc, 1)

    def test_device_flags_need_install(self):
        for flags, named in ((["--logcat"], "--logcat"), (["--reinstall"], "--reinstall"), (["--serial", "X"], "--serial"), (["--adb", "/x/adb"], "--adb"),
                             (["--logcat", "--reinstall"], "--reinstall, --logcat")):
            rc, _, err = run_cli("test-bundle", self.signed, *flags)
            self.assertEqual(rc, 1, flags)
            self.assertIn("without --install there is no device for " + named, err)

    def test_bundle_flags_are_not_for_apks(self):
        apk = os.path.join(self.tmp, "x.apk")
        with zipfile.ZipFile(apk, "w") as z:
            z.writestr("AndroidManifest.xml", b"x")
        rc, _, err = run_cli("verify", apk, "--play")
        self.assertEqual(rc, 1)
        self.assertIn("Android App Bundles", err)


# ============================================================================================================== the real thing
def installed_toolchain():
    """the toolchain apkforge already has on this machine, or None"""
    try:
        tc = T.Toolchain(fetch_missing=False)
        return tc if (tc.aapt2() and tc.dexer() and tc.bundletool() and tc.java() and tc.javac()) else None
    except (ForgeError, OSError):
        return None


FAKE_ADB = r'''#!/usr/bin/env bash
# a stand-in for adb: writes every call to $FAKE_ADB_DIR/calls.log and answers like a phone that is ready
#   FAKE_ADB_STATE=none|unauthorized     what `adb devices -l` lists (default: one device that is ready)
#   FAKE_ADB_INSTALL=incompatible        the install is refused (another signing key) until the app has been uninstalled
d="${FAKE_ADB_DIR:?}"
printf '%s\t' "$@" >> "$d/calls.log"
echo >> "$d/calls.log"
if [ "${1:-}" = "-s" ]; then shift 2; fi
case "${1:-}" in
  devices)
    echo "List of devices attached"
    [ "${FAKE_ADB_STATE:-device}" = none ] || printf 'FAKE123\t%s usb:1-1 product:fake model:Pixel_Fake device:fake transport_id:1\n' "${FAKE_ADB_STATE:-device}"
    ;;
  install)
    if [ "${FAKE_ADB_INSTALL:-}" = incompatible ] && [ ! -e "$d/uninstalled" ]; then
      echo "Performing Streamed Install"
      echo "adb: failed to install ${!#}: Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: Package com.acme.demo signatures do not match previously installed version; ignoring!]" >&2
      exit 1
    fi
    cp "${!#}" "$d/installed.apk"
    echo "Performing Streamed Install"
    echo "Success"
    ;;
  uninstall)
    touch "$d/uninstalled"
    echo "Success"
    ;;
  shell)
    case "$2" in
      "am start"*)
        echo "Starting: Intent { act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER] }"
        echo "Status: ok"
        echo "LaunchState: COLD"
        echo "Activity: com.acme.demo/.MainActivity"
        echo "TotalTime: 321"
        echo "WaitTime: 330"
        echo "Complete"
        ;;
      pidof*) echo 4242 ;;
      *) echo "fake adb: no answer for: $2" >&2; exit 1 ;;
    esac
    ;;
  logcat) echo "10-27 12:00:00.000  4242  4242 I demo: hello from the fake logcat" ;;
  *) echo "fake adb: no answer for: $*" >&2; exit 1 ;;
esac
'''


class DemoBundleTests(unittest.TestCase):
    """a demo app, built by apkforge into a bundle, then read back by jarsigner and bundletool"""

    @classmethod
    def setUpClass(cls):
        cls.tc = installed_toolchain()
        if cls.tc is None:
            raise unittest.SkipTest("aapt2, D8, bundletool and a JDK are not installed (apkforge doctor --bundletool)")
        cls.tmp = tempfile.mkdtemp(prefix="apkforge-aab-test-")
        cls.proj = os.path.join(cls.tmp, "demo")
        with quiet():
            templates.create(cls.proj, "java", None, "com.acme.demo")
        try:
            if not cls.tc.android_jar(project.load(cls.proj).default_app().compileSdk):
                raise ForgeError("no android.jar")
        except ForgeError:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("the android.jar of the demo app is not installed")
        cls.pw = {"APKFORGE_STORE_PASS": "release-pw-1", "APKFORGE_UPLOAD_PASS": "upload-pw-2"}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def build(self, *flags, env=None):
        rc, out, err = run_cli("build", self.proj, "--aab", "--json", *flags, env=env)
        self.assertEqual(rc, 0, err[-1500:])
        return json.loads(out)

    def external(self, path):
        """what the real jarsigner and bundletool say about a bundle -> (jarsigner rc, bundletool rc)"""
        js = self.tc.jarsigner()
        rc1, text = util.run([js, "-verify", "-verbose:summary", "-certs", path], check=False)
        rc2, _ = util.run([self.tc.java(), "-jar", self.tc.bundletool(), "validate", "--bundle=" + path], check=False)
        return (rc1 if "jar verified" in text.lower() else -1), rc2

    def test_a_debug_bundle_is_signed_and_checked(self):
        res = self.build()
        rep = res["aab_report"]
        self.assertEqual(res["aab"], rep["path"])
        self.assertEqual((rep["signed_with"], rep["signature"]["signed"], rep["bundle"]["package"], rep["bundle"]["problems"]), ("debug", True, "com.acme.demo", []))
        self.assertTrue(any("debug certificate" in e for e in rep["play"]["errors"]))       # said, not fatal: a debug bundle is not for Google Play
        claims = {c["claim"]: c for c in rep["claims"]}
        self.assertEqual(claims["bundle-structure"]["status"], "confirmed")
        self.assertEqual(claims["bundle-manifest"]["basis"], ["internal", "bundletool"])
        self.assertEqual(claims["bundle-config"]["status"], "confirmed")
        if self.tc.jarsigner():
            self.assertEqual(claims["bundle-signature"]["basis"], ["internal", "jarsigner"])
            self.assertEqual(self.external(res["aab"]), (0, 0))                              # the real tools accept what apkforge signed

    def test_no_sign_aab(self):
        res = self.build("--no-sign-aab")
        self.assertIsNone(res["aab_report"]["signed_with"])
        self.assertFalse(signing.verify_aab(res["aab"])["signed"])
        self.assertEqual(res["aab_report"]["claims"][0]["status"], "unchecked")

    def test_verify_command(self):
        res = self.build()
        rc, out, _ = run_cli("verify", res["aab"])
        self.assertEqual(rc, 0)
        rep = json.loads(out)
        self.assertEqual((rep["signed"], rep["problems"], rep["bundle"]["package"]), (True, [], "com.acme.demo"))
        self.assertEqual(rep["oracles"]["validate"], "ok")
        self.assertEqual(run_cli("verify", res["aab"], "--play")[0], 1)                      # the debug certificate and android:debuggable
        bad = os.path.join(self.tmp, "tampered.aab")
        with zipfile.ZipFile(res["aab"]) as zin, zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zout:
            for i in zin.infolist():
                data = zin.read(i.filename)
                zout.writestr(i, data[:-1] + bytes([data[-1] ^ 1]) if i.filename == "base/resources.pb" else data)
        self.assertEqual(run_cli("verify", bad)[0], 1)
        apk = res["aab_report"]["path"].replace(".aab", ".apk")
        self.assertEqual(run_cli("verify", apk)[0], 0)                                       # an APK still goes the old way

    def test_release_with_an_upload_key_and_resource_optimization(self):
        proj = os.path.join(self.tmp, "rel")
        with quiet():
            templates.create(proj, "java", None, "com.acme.rel")
        with mock.patch.dict(os.environ, self.pw):
            self.assertEqual(run_cli("keygen", proj)[0], 0)
            self.assertEqual(run_cli("keygen", proj, "--upload")[0], 0)
            self.assertEqual(run_cli("keygen", proj, "--upload")[0], 1)                      # never replaces a section without --force
        with open(os.path.join(proj, "apkforge.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["uploadSigning"]["keystore"], "upload.p12")
        self.assertEqual(cfg["uploadSigning"]["storePasswordEnv"], "APKFORGE_UPLOAD_PASS")
        self.assertEqual(cfg["signing"]["storePasswordEnv"], "APKFORGE_STORE_PASS")
        cfg["optimizeResources"] = {"shortenPaths": True, "sparseEncoding": True, "collapseNames": True, "keep": ["layout/activity_main"]}
        with open(os.path.join(proj, "apkforge.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        rc, out, err = run_cli("build", proj, "--release", "--aab", "--json", env=self.pw)
        self.assertEqual(rc, 0, err[-1500:])
        res = json.loads(out)
        rep = res["aab_report"]
        up = signing.keystore_signer(os.path.join(proj, "upload.p12"), "upload", "upload-pw-2", "upload-pw-2").fingerprint
        rel = signing.keystore_signer(os.path.join(proj, "release.p12"), "release", "release-pw-1", "release-pw-1").fingerprint
        self.assertNotEqual(up, rel)
        self.assertEqual(rep["signed_with"], "uploadSigning")
        self.assertEqual([s["sha256"] for s in rep["signature"]["signers"]], [up])
        self.assertEqual(rep["play"], {"errors": [], "warnings": []})                         # release, not debuggable, a certificate that lives past 2033
        c = rep["bundle"]["config"]
        self.assertEqual((c["sparse_encoding"], c["collapse_resource_names"], c["no_collapse_resources"]), ("VARIANT_FOR_SDK_32", True, ["layout/activity_main"]))
        self.assertEqual({k: v["status"] for k, v in {c_["claim"]: c_ for c_ in rep["claims"]}.items()}["bundle-config"], "confirmed")
        self.assertEqual(run_cli("verify", res["aab"], "--play")[0], 0)
        self.assertEqual(self.external(res["aab"]), (0, 0))
        self.assertEqual(run_cli("verify", res["apk"])[0], 0)

    def test_test_bundle(self):
        rc, out, err = run_cli("test-bundle", self.proj, "--json")
        self.assertEqual(rc, 0, err[-1500:])
        res = json.loads(out)
        self.assertTrue(os.path.isfile(res["apk"]))
        self.assertTrue(res["apk"].endswith("-universal.apk"))
        self.assertEqual(signing.verify_apk(res["apk"])["problems"], [])
        self.assertIn("launcher-activity", {c["claim"] for c in res["claims"]})
        self.assertIsNone(res["installed"])
        rc, out, err = run_cli("test-bundle", res["aab"], "--json")                          # a bundle that exists, tried out as it is
        self.assertEqual(rc, 0, err[-1500:])
        self.assertEqual(json.loads(out)["package"], "com.acme.demo")

    # ------------------------------------------------------------------------------------ --install, against a stand-in for adb (no phone here)
    _try_aab = None

    def bundle_to_try(self):
        """a signed debug bundle of its own (the other tests rebuild and overwrite theirs)"""
        cls = type(self)
        if cls._try_aab is None:
            res = self.build()
            cls._try_aab = os.path.join(self.tmp, "try.aab")
            shutil.copy(res["aab"], cls._try_aab)
        return cls._try_aab

    def fake_adb(self, **env):
        """-> (the adb script, its folder with calls.log, the environment that makes run_cli use it)"""
        d = tempfile.mkdtemp(prefix="fake-adb-", dir=self.tmp)
        path = os.path.join(d, "adb")
        with open(path, "w", encoding="utf-8") as f:
            f.write(FAKE_ADB)
        os.chmod(path, 0o755)
        return path, d, dict(env, FAKE_ADB_DIR=d)

    @staticmethod
    def adb_calls(d):
        with open(os.path.join(d, "calls.log"), encoding="utf-8") as f:
            return [line.rstrip("\t\n").split("\t") for line in f if line.strip()]

    def test_install_and_start_on_a_phone(self):
        adb, d, env = self.fake_adb()
        rc, out, err = run_cli("test-bundle", self.bundle_to_try(), "--install", "--logcat", "--json", "--adb", adb, env=env)
        self.assertEqual(rc, 0, err[-1500:])
        res = json.loads(out)                                      # stdout is that one JSON document and nothing else: the app's log goes to stderr
        self.assertEqual(res["installed"]["serial"], "FAKE123")
        self.assertTrue(res["installed"]["component"].startswith("com.acme.demo/"), res["installed"])
        self.assertIn("hello from the fake logcat", err)
        self.assertIn("started com.acme.demo/", err)
        calls = self.adb_calls(d)
        self.assertEqual(calls[0], ["devices", "-l"])                                          # the phone is found first, then everything carries -s SERIAL
        self.assertEqual(calls[1], ["-s", "FAKE123", "install", "-r", res["apk"]])
        self.assertEqual(calls[2][:3], ["-s", "FAKE123", "shell"])
        self.assertTrue(calls[2][3].startswith("am start -W -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -n com.acme.demo/"), calls[2])
        self.assertEqual(calls[3], ["-s", "FAKE123", "shell", "pidof com.acme.demo"])
        self.assertEqual(calls[4], ["-s", "FAKE123", "logcat", "--pid=4242", "-v", "threadtime"])
        self.assertEqual(rd(os.path.join(d, "installed.apk")), rd(res["apk"]))                # what the phone was given is the universal APK, and it verifies
        self.assertEqual(signing.verify_apk(res["apk"])["problems"], [])

    def test_a_phone_that_refuses_the_update(self):
        aab = self.bundle_to_try()
        adb, d, env = self.fake_adb(FAKE_ADB_INSTALL="incompatible")
        rc, out, err = run_cli("test-bundle", aab, "--install", "--adb", adb, env=env)
        self.assertEqual(rc, 1)
        self.assertIn("INSTALL_FAILED_UPDATE_INCOMPATIBLE", err)
        self.assertIn("--reinstall", err)                                       # told, never done on its own: uninstalling deletes the app's data
        self.assertNotIn("uninstall", [c[2] for c in self.adb_calls(d) if c[:1] == ["-s"]])
        adb, d, env = self.fake_adb(FAKE_ADB_INSTALL="incompatible")
        rc, out, err = run_cli("test-bundle", aab, "--install", "--reinstall", "--adb", adb, env=env)
        self.assertEqual(rc, 0, err[-1500:])
        self.assertEqual([c[2] for c in self.adb_calls(d) if c[:1] == ["-s"]], ["install", "uninstall", "install", "shell"])
        self.assertIn("reinstalled", out + err)                                  # (the progress lines are on stdout unless --json asks for them on stderr)

    def test_no_phone_no_build(self):
        proj = os.path.join(self.tmp, "nophone")
        with quiet():
            templates.create(proj, "java", None, "com.acme.nophone")
        for state, words in (("none", "no device or emulator is connected"), ("unauthorized", "FAKE123 is unauthorized")):
            adb, d, env = self.fake_adb(FAKE_ADB_STATE=state)
            rc, out, err = run_cli("test-bundle", proj, "--install", "--adb", adb, env=env)
            self.assertEqual(rc, 1)
            self.assertIn(words, err)
            self.assertEqual(self.adb_calls(d), [["devices", "-l"]])
            self.assertFalse(os.path.exists(os.path.join(proj, "build")))                  # found out before minutes were spent on the build


if __name__ == "__main__":
    unittest.main()
