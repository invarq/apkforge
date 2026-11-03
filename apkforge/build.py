"""The build pipeline. One content-addressed action cache; every stage is a pure function of its inputs.

app / library module  ->  dependency resolution -> merged manifest -> aapt2 compile + link (R.java) -> BuildConfig ->
kotlinc/javac -> API guards (JDK-only APIs; platform APIs newer than minSdk) -> D8 (or R8) -> deterministic zip + 16 KB .so alignment -> pure-Python v1/v2/v3
signing -> verification (own verifier, manifest-vs-dex check, optional Google oracles).  Library modules produce AARs.
"""
import base64
import glob
import json
import os
import re
import shutil
import struct
import tempfile
import zipfile

from . import aab as aabmod, apiguard, arsc, classfile, deps, elfcheck, native, react, resopt, signing, toolchain, util, viewbinding, zipw
from . import manifest as M
from .project import topo
from .util import ForgeError, detail, info, ok, run, stage, warn

# stored (never deflated) when packaging: media that is already compressed
NO_COMPRESS = {"jpg", "jpeg", "png", "gif", "webp", "wav", "mp2", "mp3", "ogg", "aac", "mpg", "mpeg", "mid", "midi", "smf",
               "jet", "rtttl", "imy", "xmf", "mp4", "m4a", "m4v", "3gp", "3gpp", "3g2", "3gpp2", "amr", "awb", "wma", "wmv",
               "webm", "mkv", "arsc"}
SIG_FILE = re.compile(r"^META-INF/([^/]+\.(SF|RSA|DSA|EC)|MANIFEST\.MF|INDEX\.LIST)$", re.I)
LICENSE_FILE = re.compile(r"^META-INF/(LICENSE|NOTICE|DEPENDENCIES)[^/]*$", re.I)
# first path components of an App Bundle module that bundletool owns: a Java resource with one of these names cannot live under root/
AAB_RESERVED = {"AndroidManifest.xml", "resources.arsc", "resources.pb", "dex", "res", "assets", "lib", "manifest", "root"}

DEFAULT_R8_RULES = """# apkforge defaults (condensed from the Android Gradle plugin's proguard-android-optimize.txt)
-keepattributes Signature,InnerClasses,EnclosingMethod,*Annotation*,Exceptions,SourceFile,LineNumberTable
-keepclasseswithmembernames,includedescriptorclasses class * { native <methods>; }
-keepclassmembers enum * { public static **[] values(); public static ** valueOf(java.lang.String); }
-keepclassmembers class * implements android.os.Parcelable { public static final ** CREATOR; }
-keepclassmembers class * implements java.io.Serializable {
  static final long serialVersionUID; private static final java.io.ObjectStreamField[] serialPersistentFields;
  private void writeObject(java.io.ObjectOutputStream); private void readObject(java.io.ObjectInputStream);
  java.lang.Object writeReplace(); java.lang.Object readResolve(); }
-keepclassmembers class * extends android.view.View { void set*(***); *** get*(); }
-keep public class * extends android.app.Application
-keep public class * extends android.app.Activity
-keep public class * extends android.app.Service
-keep public class * extends android.content.BroadcastReceiver
-keep public class * extends android.content.ContentProvider
-keep public class * extends android.app.backup.BackupAgentHelper
"""


# ------------------------------------------------------------------------------------------------------------- cache
class Cache:
    """Content-addressed store of stage outputs. `explain` remembers the inputs of the last build to say what changed."""

    def __init__(self, root, explain_file):
        self.root, self.explain_file = root, explain_file
        os.makedirs(root, exist_ok=True)
        try:
            self.prev = json.load(open(explain_file))
        except (OSError, ValueError):
            self.prev = {}
        self.now, self.stats = {}, {"hit": 0, "miss": 0}

    def action(self, name, inputs):
        stamp = zipw.stamp_cache_key()
        if stamp and isinstance(inputs, dict):
            # SOURCE_DATE_EPOCH is in effect: some actions store zip bytes (classes.jar, which an AAR carries) and those bytes contain the stamp, so a
            # build with another stamp must not replay them. Unset, nothing is added: the keys (and the caches) of builds without it do not change.
            inputs = dict(inputs, zipStamp=stamp)
        return Action(self, name, inputs)

    def save(self):
        with open(self.explain_file, "w") as f:
            json.dump(self.now, f, indent=1, sort_keys=True)


class Action:
    def __init__(self, cache, name, inputs):
        self.cache, self.name, self.inputs = cache, name, inputs
        self.key = util.sha256_bytes(util.canon([name, inputs]).encode())
        self.final = os.path.join(cache.root, self.key[:2], self.key)
        self.hit, self.dir = False, self.final

    def __enter__(self):
        c = self.cache
        self.hit = os.path.isfile(os.path.join(self.final, ".done"))
        c.now[self.name] = {"key": self.key[:16], "hit": self.hit, "inputs": self.inputs}
        c.stats["hit" if self.hit else "miss"] += 1
        if self.hit:
            detail("cached   %s" % self.name)
            return self
        prev = c.prev.get(self.name)
        if prev:
            ch = sorted(k for k in set(self.inputs) | set(prev["inputs"]) if util.canon(self.inputs.get(k)) != util.canon(prev["inputs"].get(k)))
            detail("rebuild  %s  (changed: %s)" % (self.name, ", ".join(ch) or "?"))
        else:
            detail("build    %s  (not built before)" % self.name)
        self.dir = self.final + ".tmp"
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir)
        return self

    def __exit__(self, et, ev, tb):
        if self.hit:
            return False
        if et is not None:
            shutil.rmtree(self.dir, ignore_errors=True)
            return False
        open(os.path.join(self.dir, ".done"), "w").close()
        shutil.rmtree(self.final, ignore_errors=True)
        os.replace(self.dir, self.final)
        self.dir = self.final
        return False

    def path(self, *p):
        return os.path.join(self.dir, *p)


# ----------------------------------------------------------------------------------------------------------- helpers
def jar_dirs(srcs, out, exclude=None):
    zb = zipw.ZipBuilder()
    seen = set()
    for src in srcs:
        for p in util.tree_files(src):
            rel = os.path.relpath(p, src).replace(os.sep, "/")
            if rel in seen or (exclude and exclude(rel)):
                continue
            seen.add(rel)
            with open(p, "rb") as f:
                zb.add(rel, f.read())
    with open(out, "wb") as f:
        f.write(zb.finish())


# kapt 1.x on JDK 16+: javac's internals are encapsulated; kapt reaches into them
KAPT_ADD_OPENS = ["--add-opens=jdk.compiler/com.sun.tools.javac.%s=ALL-UNNAMED" % p for p in
                  ("code", "comp", "file", "main", "model", "parser", "processing", "tree", "util", "jvm")]


PROJECT_DIR_TOKEN = "${projectDir}"


def expand_args(m, args):
    """kspArgs / kaptArgs values may carry the token ${projectDir} (the module's directory; Gradle scripts write "$projectDir/schemas" for Room's schema location,
    and import-gradle keeps that as the token so that apkforge.json stays portable). Expanded for the command line only: the cache keys keep the token."""
    return {k: (v.replace(PROJECT_DIR_TOKEN, m.dir) if isinstance(v, str) else v) for k, v in args.items()}


def kapt_encode(opts):
    """kapt's `apoptions` / `javacArguments` value: base64 of a java.io.ObjectOutputStream holding int n, then n x (UTF key, UTF value)."""
    body = struct.pack(">i", len(opts))
    for k, v in opts.items():
        for t in (str(k), str(v)):
            b = t.encode("utf-8")
            body += struct.pack(">H", len(b)) + b
    out = b"\xac\xed\x00\x05"
    for i in range(0, len(body), 1024):                      # block-data records, at most 1024 bytes each
        ch = body[i:i + 1024]
        out += (b"\x77" + bytes([len(ch)]) if len(ch) <= 255 else b"\x7a" + struct.pack(">i", len(ch))) + ch
    return base64.b64encode(out).decode("ascii")


def jar_classes(path):
    out = {}
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.endswith(".class") and not n.endswith("module-info.class") and not n.startswith("META-INF/"):
                out[n[:-6]] = z.read(n)
    return out


def _jlit(t, v):
    t = (t or "String")
    if t == "String":
        return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    if t == "boolean":
        return "true" if str(v).lower() == "true" else "false"
    if t == "long":
        return "%dL" % int(v)
    if t == "int":
        return str(int(v))
    if t in ("float", "double"):
        return repr(float(v)) + ("f" if t == "float" else "")
    return str(v)


def gen_buildconfig(m, ns, appid, release):
    fields = [("boolean", "DEBUG", "false" if release else "true"), ("String", "APPLICATION_ID", _jlit("String", appid)),
              ("String", "BUILD_TYPE", _jlit("String", "release" if release else "debug")),
              ("int", "VERSION_CODE", str(int(m.versionCode))), ("String", "VERSION_NAME", _jlit("String", m.versionName))]
    for name, spec in sorted(m.buildConfigFields.items()):
        t, v = (spec.get("type", "String"), spec.get("value")) if isinstance(spec, dict) else ("String", spec)
        fields.append((t, name, _jlit(t, v)))
    body = "\n".join("  public static final %s %s = %s;" % f for f in fields)
    return "package %s;\n\n/** Generated by apkforge. Do not edit. */\npublic final class BuildConfig {\n%s\n  private BuildConfig() {}\n}\n" % (ns, body)


def gen_res_values(m):
    if not m.resValues:
        return None
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<resources>"]
    for name, spec in sorted(m.resValues.items()):
        t, v = (spec.get("type", "string"), spec.get("value")) if isinstance(spec, dict) else ("string", spec)
        v = str(v).replace("&", "&amp;").replace("<", "&lt;").replace("'", "\\'")
        lines.append('  <%s name="%s" translatable="false">%s</%s>' % (t, name, v, t) if t == "string" else '  <%s name="%s">%s</%s>' % (t, name, v, t))
    lines.append("</resources>")
    return "\n".join(lines) + "\n"


class Prep:
    """Resolved dependencies of one module."""
    compile = runtime = ()
    processors = kapt = ksp = ()          # jars of the javac annotation processors / kapt processors / KSP processors (plain-JVM variants)
    deps = ()
    features = {}                         # {"compose": bool, "serialization": bool, "parcelize": bool}: the Kotlin compiler plugins this module compiles with
    js = None                             # the JavaScript bundle of a React Native app (Builder.react_bundle): {"plan", "meta", "assets", "res", ...}; None for every other module
    native = None                         # what native.build() made for the module (Builder.native_build): {"dir", "libs", ...}; None for a module without a "native" setting


class _Incomplete(Exception):
    """raised inside a cache action so that a min-API verdict that could not be completed is not stored"""


# ------------------------------------------------------------------------------------------------- what verify claims
# Every verification claim carries its basis. status: "confirmed" = an external verifier agreed, "internal" = apkforge's own code only,
# "unchecked" = nobody checked it. The one `verified:` line shows `<text> [<tag>]` per claim; build --json and `apkforge verify` keep the dicts under "claims".
_SCHEME_LINE = re.compile(r"^Verified using v(\d) scheme \([^)]*\): (true|false)\s*$", re.M)


def _claim(claim, detail, text, status, basis=(), note=""):
    return {"claim": claim, "detail": detail, "text": text, "status": status, "basis": list(basis), "note": note}


def claim_tag(c):
    if c["status"] == "confirmed":
        return " + ".join(c["basis"])
    return ("internal only" if c["status"] == "internal" else "not checked") + ("; " + c["note"] if c["note"] else "")


def render_claims(claims):
    return "; ".join("%s [%s]" % (c["text"], claim_tag(c)) for c in claims)


def _carries_v1(apk):
    try:
        with zipfile.ZipFile(apk) as z:
            return "META-INF/MANIFEST.MF" in z.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def run_apk_oracles(tc, apk, min_sdk=None, use_apksigner=True, found=None):
    """Run the external verifiers that are installed against one APK: apksigner (the signature) and zipalign (the alignment).
    found = tc.oracles() when the caller already asked (oracles() may run apt-get under --allow-apt: ask once).
    -> {"apksigner": r, "zipalign": r}; r["state"] is "ok" | "rejected" | "missing" | "disabled"; r["text"] is the tool's output (without JVM start-up notices)."""
    out = {}
    ap = (found if found is not None else tc.oracles()).get("apksigner")
    if not use_apksigner:
        out["apksigner"] = {"state": "disabled", "text": ""}
    elif not ap:
        out["apksigner"] = {"state": "missing", "text": ""}
    else:
        sidecar = apk + signing.V4_SUFFIX
        v4_file = sidecar if os.path.isfile(sidecar) else None
        cmd = [ap, "verify", "-v"] + (["--min-sdk-version", str(min_sdk)] if min_sdk else [])
        # apksigner does not look for <apk>.idsig by itself: without --v4-signature-file it prints `v4 ... false` and exits 0, whatever the file holds
        rc, text = run(cmd + (["--v4-signature-file", v4_file] if v4_file else []) + [apk], check=False)
        text = util.tool_output(text)
        asked = v4_file is not None
        if asked and rc != 0 and "Unsupported option" in text:
            # an apksigner older than build-tools 30 does not know the option and cannot check v4: ask again without it, and the v4 claim stays internal
            asked = False
            rc, text = run(cmd + [apk], check=False)
            text = util.tool_output(text)
        schemes = {int(n): v == "true" for n, v in _SCHEME_LINE.findall(text)} or None      # the schemes apksigner itself says it verified
        if rc == 0 and schemes is not None and not schemes.get(1) and _carries_v1(apk):
            # apksigner skips the v1 (JAR) check for an APK whose minSdk is 24 or more, so a v1 signature the APK does carry stayed "internal only".
            # Ask once more as if the APK still had to run on API 23: apksigner then checks v1 as well, and a rejection is reported like any other.
            rc1, text1 = run([ap, "verify", "-v", "--min-sdk-version", "23", apk], check=False)      # about v1 only: no v4 file is given here, so its v4 line would say false
            text1 = util.tool_output(text1)
            if rc1 == 0:
                schemes.update({int(n): v == "true" for n, v in _SCHEME_LINE.findall(text1) if int(n) != 4})
            else:
                rc, text = rc1, text1
                schemes = {int(n): v == "true" for n, v in _SCHEME_LINE.findall(text)} or None
        v4 = None
        if v4_file:
            said = (schemes or {}).get(4)            # a v4 file apksigner rejects still gives exit status 0 (v2 and v3 verify), so its own v4 line is the answer
            v4 = {"file": v4_file, "state": "unsupported" if not asked else "ok" if said is True else "rejected" if said is False else "unchecked"}
        out["apksigner"] = {"state": "ok" if rc == 0 else "rejected", "text": text, "schemes": schemes, "v4": v4}
    za = tc.zipalign()
    if not za:
        out["zipalign"] = {"state": "missing", "text": ""}
    else:
        rc, text = run([za, "-c", "-p", "4", apk], check=False)
        if rc == 0:
            out["zipalign"] = {"state": "ok", "text": text}
        else:
            _, vout = run([za, "-c", "-v", "-p", "4", apk], check=False)      # without -v zipalign prints nothing at all
            out["zipalign"] = {"state": "rejected", "text": vout or text, "rc": rc, "bad": [l.strip() for l in vout.splitlines() if "(BAD" in l]}
    return out


def oracle_rejections(ora):
    """-> [message], one per external verifier that rejected the APK (apksigner first), in the wording the build has always used."""
    out = []
    if ora["apksigner"]["state"] == "rejected":
        out.append("Google's apksigner rejects the APK:\n" + ora["apksigner"]["text"][:600])
    v4 = ora["apksigner"].get("v4")
    if ora["apksigner"]["state"] == "ok" and v4 and v4["state"] == "rejected":
        out.append("Google's apksigner does not accept the v4 signature %s: it prints 'Verified using v4 scheme: false' for it, and still exits 0 because v2 and v3 verify" % os.path.basename(v4["file"]))
    za = ora["zipalign"]
    if za["state"] == "rejected":
        bad = za["bad"]
        if bad:
            head = "zipalign check failed: %d stored entr%s not aligned (offset, name, bytes past the boundary):" % (len(bad), "y is" if len(bad) == 1 else "ies are")
            body = bad[:8] + (["... and %d more" % (len(bad) - 8)] if len(bad) > 8 else [])
        else:
            head = "zipalign check failed (exit %d):" % za["rc"]
            body = [za["text"].strip()[:300] or "zipalign printed nothing"]
        out.append(head + "\n    " + "\n    ".join(body))
    return out


def signature_claims(rep, ora):
    """the signature claim, split by scheme: the schemes apksigner itself verified are `confirmed`, the others stay `internal`"""
    present = [n for n in (1, 2, 3, 4) if rep.get("v%d" % n) is True]
    if not present:
        return []
    names = lambda ns: " ".join("v%d" % n for n in ns)
    ap = ora["apksigner"]
    if ap["state"] != "ok":
        return [_claim("signature", names(present), "signature " + names(present), "internal", ("internal",),
                       "apksigner disabled" if ap["state"] == "disabled" else "apksigner not found")]
    seen = ap.get("schemes")
    v4_state = (ap.get("v4") or {}).get("state")

    def checked(n):
        if n == 4:                  # exit status 0 says nothing about v4 (a rejected file still gives 0): only apksigner's own `v4 ... true`, asked for with the file, counts
            return v4_state == "ok"
        return seen is None or bool(seen.get(n))

    yes = [n for n in present if checked(n)]
    out = []
    if yes:
        out.append(_claim("signature", names(yes), "signature " + names(yes), "confirmed", ("internal", "apksigner")))
    notes = {}                      # one claim per reason, so that each note says why that scheme stayed internal
    for n in present:
        if n not in yes:
            why = {"unsupported": "this apksigner cannot check v4 files (build-tools 30 or newer can)", "rejected": "apksigner does not accept it"}.get(v4_state if n == 4 else None, "apksigner did not check it")
            notes.setdefault(why, []).append(n)
    for why, ns in notes.items():
        out.append(_claim("signature", names(ns), "signature " + names(ns), "internal", ("internal",), why))
    return out


def alignment_claim(ora):
    if ora["zipalign"]["state"] == "ok":
        return _claim("zip-alignment", "", "zip alignment", "confirmed", ("zipalign",))
    return _claim("zip-alignment", "", "zip alignment", "unchecked", (), "zipalign not found")


def elf_report(found, apk):
    """The 16 KB page-size checks (elfcheck.py) for the native libraries of one APK, with the independent readers that are installed: readelf for the ELF
    headers, `zipalign -P 16` for where each stored library starts in the zip.  found = tc.oracles().
    -> {"libs": [...], "claims": [...], "rule": [str], "errors": [str], "warnings": [str]}
    rule = a 64-bit library breaks a 16 KB rule (how hard that fails is the caller's choice: elfAlignment); errors = apkforge's reader and an independent one
    disagree (always a failure); warnings = odd but no rule broken.  An APK with no lib/<abi>/*.so entry gives empty lists and no claim."""
    out = {"libs": [], "claims": [], "rule": [], "errors": [], "warnings": []}
    try:
        libs = elfcheck.scan_apk(apk)
    except (OSError, zipfile.BadZipFile):
        return out                          # a file that is no zip is reported by the signature check
    if not libs:
        return out
    out["libs"] = libs
    found = found or {}
    readelf, zipalign = found.get("readelf"), found.get("zipalign")
    required = []
    for r in libs:
        if "error" in r:
            out["warnings"].append("%s: %s" % (r["name"], r["error"]))
            continue
        out["warnings"] += ["%s: %s" % (r["name"], w) for w in r["warnings"]]
        if readelf:
            rows = elfcheck.readelf_rows(readelf, r["_data"])
            if rows is None:
                r["readelf"] = "unreadable"
            else:
                diff = elfcheck.compare(r["_data"], rows)
                r["readelf"] = "differs" if diff else "agrees"
                if diff:
                    out["errors"].append("%s: apkforge's ELF reader and readelf disagree: %s" % (r["name"], diff[0]))
        if r["required"]:
            required.append(r)
            out["rule"] += ["%s: %s" % (r["name"], f["text"]) for f in r["findings"]]
    stored = [r for r in required if r["stored"]]
    mine = dict(elfcheck.zip_offsets(apk, {r["name"] for r in stored})) if stored else {}
    for n, off in sorted(mine.items()):
        out["rule"].append("%s: the stored entry starts at byte %s of the APK, not on a 16 KB boundary (zipalign -P 16 -p 4 fixes it)" % (n, off if off >= 0 else "?"))
    zip_state = "missing"                   # what zipalign said about the stored libraries: ok | rejected | missing | unusable
    if stored and zipalign:
        rc, text = run([zipalign, "-c", "-P", "16", "-v", "4", apk], check=False)
        bad = {m.group(1) for m in re.finditer(r"^\s*\d+\s+(lib/\S+\.so)\s+\(BAD", text, re.M)}
        theirs = bad & {r["name"] for r in stored}
        if rc != 0 and not bad:
            zip_state = "unusable"          # an older zipalign without -P, or one that failed for another reason: no opinion
        elif theirs != set(mine):
            out["errors"].append("zipalign -P 16 and apkforge disagree about the zip alignment of the native libraries (zipalign: %s; apkforge: %s)" % (
                ", ".join(sorted(theirs)) or "all aligned", ", ".join(sorted(mine)) or "all aligned"))
            zip_state = "differs"
        else:
            zip_state = "rejected" if theirs else "ok"
    if required and not out["rule"] and not out["errors"]:
        n = len(required)
        text = "16 KB ELF alignment of %d 64-bit native librar%s" % (n, "y" if n == 1 else "ies")
        states = {r.get("readelf") for r in required}
        if states == {"agrees"}:
            out["claims"].append(_claim("elf-page-size", str(n), text, "confirmed", ("internal", "readelf")))
        else:
            why = "readelf not found" if not readelf else "readelf could not read it"
            out["claims"].append(_claim("elf-page-size", str(n), text, "internal", ("internal",), why))
        if stored:
            ztext = "16 KB zip alignment of %d stored native librar%s" % (len(stored), "y" if len(stored) == 1 else "ies")
            if zip_state == "ok":
                out["claims"].append(_claim("zip-page-alignment", str(len(stored)), ztext, "confirmed", ("internal", "zipalign")))
            else:
                out["claims"].append(_claim("zip-page-alignment", str(len(stored)), ztext, "internal", ("internal",),
                                            "zipalign not found" if zipalign is None else "zipalign -P 16 could not check it"))
    return out


def apply_elf_policy(policy, elf, apk):
    """What the project's elfAlignment setting does with elf_report() of the APK that was just written.  -> the claims to add to the build report.
    Warns about everything that is allowed to pass; raises ForgeError when the build must fail.  Under "error" the APK is removed first: a gate that rejects
    an artifact must not leave it in build/outputs for a pipeline that looks at the file and not at the exit status.  Readers that disagree are a
    verification failure under every policy and, like the other verification failures, keep the APK (that says nothing about the file itself)."""
    for w in elf["warnings"]:
        warn(w)
    if elf["errors"]:
        raise ForgeError("verification failed:\n    " + "\n    ".join(elf["errors"]))
    if elf["rule"] and policy == "error":
        for f in (apk, apk + signing.V4_SUFFIX):             # a v4 sidecar without its APK would be an orphan
            try:
                os.remove(f)
            except OSError:
                pass
        raise ForgeError("16 KB page-size check failed (elfAlignment is \"error\"; the APK was not kept):\n    " + "\n    ".join(elf["rule"]))
    for line in elf["rule"]:
        warn("16 KB page size: " + line)
    return elf["claims"]


def _remove_sidecar(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise ForgeError("cannot remove the old %s (%s): a v4 signature of an earlier build must not stay beside a new APK" % (path, e))


def write_apk(apk, blob, signer, v4):
    """Write the signed APK `blob` and, when v4 is on, its sidecar `<apk>.idsig`.  -> the sidecar's path, or None.
    A sidecar is valid for the exact bytes it was made from and for nothing else, so the one of an earlier build is removed BEFORE the new APK is written
    (whether v4 is on or not): whatever fails afterwards leaves an APK without a sidecar, never an APK beside another build's."""
    sidecar = apk + signing.V4_SUFFIX
    _remove_sidecar(sidecar)
    with open(apk, "wb") as f:
        f.write(blob)
    if not v4:
        return None
    idsig = signing.v4_signature(blob, signer)         # a refusal (no v2/v3 block, another certificate, a signature too large for Android) leaves no sidecar
    with open(sidecar, "wb") as f:
        f.write(idsig)
    return sidecar


_PACKAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


def read_apk_manifest(aapt2, apk):
    """The manifest inside an APK as aapt2 reads it (`dump packagename`, `dump xmltree`): -> (package, root element), or (None, None) when there is no aapt2
    or it cannot read the file.  This is the independent reader behind `run`'s claims."""
    if not aapt2:
        return None, None
    rc1, pkg = run([aapt2, "dump", "packagename", apk], check=False)
    rc2, xml = run([aapt2, "dump", "xmltree", "--file", "AndroidManifest.xml", apk], check=False)
    names = [l.strip() for l in util.tool_output(pkg).splitlines() if _PACKAGE_NAME.match(l.strip())]
    if rc1 != 0 or rc2 != 0 or not names:
        return None, None
    try:
        return names[-1], M.parse_xmltree(xml)
    except ForgeError:
        return None, None


def launch_claims(appid, entries, chosen, apk_package, apk_root):
    """What `run` is about to start, and who agrees.  entries = the launchers of apkforge's merged manifest (launchers()); chosen = the entry that will start,
    or {"name": ..., "explicit": True} for --activity; apk_package, apk_root = read_apk_manifest() (None, None when no second reader exists).
    -> [claims] (confirmed = both readers agree, internal = only apkforge read it).  Raises ForgeError when aapt2 reads the APK differently."""
    claims = []
    why = "aapt2 not found or could not read the APK"
    text = "applicationId %s" % appid
    if apk_package is None:
        claims.append(_claim("application-id", appid, text, "internal", ("internal",), why))
    elif apk_package != appid:
        raise ForgeError("verification failed:\n    aapt2 reads the package of the APK as %s, apkforge built it as %s" % (apk_package, appid))
    else:
        claims.append(_claim("application-id", appid, text, "confirmed", ("internal", "aapt2")))
    name = chosen["name"]
    if chosen.get("explicit"):
        text = "activity %s (--activity)" % name
        if apk_root is None:
            claims.append(_claim("start-activity", name, text, "unchecked", (), why))
        else:
            app = apk_root.find("application")
            declared = [e.get(M.A + "name") for e in (app if app is not None else []) if e.tag in ("activity", "activity-alias")]
            if name not in declared:
                raise ForgeError("the APK declares no activity %s%s" % (name, " (it declares: %s)" % ", ".join(declared) if declared else " (it declares none)"))
            claims.append(_claim("start-activity", name, text, "confirmed", ("aapt2",)))
        return claims
    text = "launcher activity %s%s" % (name, " (alias of %s)" % chosen["alias_of"] if chosen.get("alias_of") else "")
    if apk_root is None:
        claims.append(_claim("launcher-activity", name, text, "internal", ("internal",), why))
        return claims
    theirs = M.launchers(apk_root)
    if theirs != entries:
        show = lambda lst: ", ".join("%s (%s)" % (e["name"], e["category"]) for e in lst) or "none"
        raise ForgeError("verification failed:\n    the launcher activities of apkforge's merged manifest (%s) differ from those of the manifest inside the APK, as aapt2 reads it (%s)"
                         % (show(entries), show(theirs)))
    claims.append(_claim("launcher-activity", name, text, "confirmed", ("internal", "aapt2")))
    return claims


# ------------------------------------------------------------------------------------------------------- Android App Bundles (aab.py)
def bundle_rejections(sig, ora, cross):
    """-> [message]: one per external reader that rejects the bundle (jarsigner only for a bundle that is signed: it exits 0 for an unsigned jar and says so in words), and one per place
    where apkforge's own decoders and bundletool's `dump manifest` / `dump config` disagree."""
    out = []
    if sig["signed"] and ora["jarsigner"]["state"] == "rejected":
        out.append("jarsigner -verify rejects the bundle's signature:\n" + ora["jarsigner"]["text"].strip()[-600:])
    if ora["validate"]["state"] == "rejected":
        out.append("bundletool validate rejects the bundle:\n" + ora["validate"]["text"].strip()[:600])
    for key, what in (("manifest", "the manifest"), ("config", "BundleConfig.pb")):
        if cross[key]["diffs"]:
            out.append("apkforge reads %s of the bundle differently from bundletool (`dump %s`): %s" % (what, key, "; ".join(cross[key]["diffs"][:4])))
    return out


def bundle_claims(sig, bundle, ora, cross):
    """What was checked about a bundle and by whom, in the model of the APK's claims: [internal + jarsigner] = apkforge's reader and the JDK's agree; [bundletool] = only bundletool
    looked; [internal only; ...] = only apkforge read it; [not checked; ...] = nobody did."""
    claims = []
    if sig["signed"]:
        algs = "/".join(sorted({s["digest"] for s in sig["signers"] if s.get("digest")}))
        text = "bundle signature (JAR%s)" % (", " + algs if algs else "")
        if ora["jarsigner"]["state"] == "ok":
            claims.append(_claim("bundle-signature", "jar", text, "confirmed", ("internal", "jarsigner")))
        else:
            claims.append(_claim("bundle-signature", "jar", text, "internal", ("internal",), "jarsigner not found (it comes with a JDK)"))
    if ora["validate"]["state"] == "ok":
        claims.append(_claim("bundle-structure", "", "bundle structure", "confirmed", ("bundletool",)))
    else:
        claims.append(_claim("bundle-structure", "", "bundle structure", "unchecked", (), "bundletool not found (apkforge doctor --bundletool)"))
    for key, text in (("manifest", "manifest of %s (versionCode %s, minSdk %s, targetSdk %s)" % (bundle["package"], bundle["versionCode"], bundle["minSdk"], bundle["targetSdk"])),
                      ("config", "bundle config: " + aabmod.describe_decoded(bundle["config"] or {}))):
        if cross[key]["state"] == "agree":
            claims.append(_claim("bundle-" + key, "", text, "confirmed", ("internal", "bundletool")))
        else:
            claims.append(_claim("bundle-" + key, "", text, "internal", ("internal",), "bundletool not found (apkforge doctor --bundletool)"))
    return claims


def verify_bundle(tc, path, unsigned_ok=False):
    """Every reader of an Android App Bundle that is available, and what they say: apkforge's own (signing.verify_aab for the JAR signature, aab.read_bundle for the proto manifest and
    BundleConfig.pb) always; jarsigner and bundletool when they are installed (nothing is downloaded here: pass a toolchain that may fetch only if you want that).
    unsigned_ok: the bundle is unsigned on purpose (`build --no-sign-aab`, `verify --allow-unsigned`).
    -> {"signature": signing.verify_aab's report, "bundle": aab.read_bundle's, "play": {"errors", "warnings"}, "oracles": {reader: state}, "problems": [...], "claims": [...]}"""
    sig = signing.verify_aab(path)
    bundle, root = aabmod.read_bundle(path)
    ora = aabmod.run_bundle_oracles(tc, path)
    cross = aabmod.cross_check(bundle, root, ora)
    problems = ([] if (unsigned_ok and not sig["signed"]) else list(sig["problems"])) + list(bundle["problems"]) + bundle_rejections(sig, ora, cross)
    problems = list(dict.fromkeys(problems))
    claims = [] if problems else bundle_claims(sig, bundle, ora, cross)
    if unsigned_ok and not sig["signed"] and not problems:
        claims.insert(0, _claim("bundle-signature", "jar", "bundle signature", "unchecked", (), "the bundle is unsigned on purpose"))
    return {"signature": sig, "bundle": bundle, "play": aabmod.play_findings(bundle, sig, unsigned_ok), "oracles": {k: v["state"] for k, v in ora.items()},
            "problems": problems, "claims": claims}


def universal_check(tc, aab_path, apk_path, signer, min_sdk, work):
    """`apkforge test-bundle`: bundletool's universal APK of the bundle (aab.universal_apk), signed with `signer`, written to `apk_path` and checked like any APK of apkforge's: its own
    signature and alignment readers, apksigner and zipalign when they are installed, and aapt2's reading of its manifest against the bundle's (package and launcher activity).
    -> {"apk", "size", "sha256", "package", "launchers", "claims"}.  ForgeError when a reader rejects it."""
    bundle, _root = aabmod.read_bundle(aab_path)
    if bundle["problems"]:
        raise ForgeError("cannot read %s as an App Bundle: %s" % (os.path.basename(aab_path), "; ".join(bundle["problems"])))
    min_sdk = bundle["minSdk"] if min_sdk is None else min_sdk
    blob, _built = aabmod.universal_apk(tc, aab_path, signer, min_sdk, work)
    write_apk(apk_path, blob, signer, False)
    rep = signing.verify_apk(apk_path)
    if rep["problems"]:
        raise ForgeError("signature self-check failed: " + "; ".join(rep["problems"]))
    ora = run_apk_oracles(tc, apk_path, min_sdk, True, tc.oracles())
    for reason in oracle_rejections(ora):
        raise ForgeError(reason)
    claims = signature_claims(rep, ora) + [alignment_claim(ora)]
    try:
        aapt2 = tc.aapt2()
    except ForgeError:
        aapt2 = None
    package, root = read_apk_manifest(aapt2, apk_path)
    entries, appid = bundle["launchers"], bundle["package"]
    chosen = M.pick_launcher(entries)
    if chosen:
        claims += launch_claims(appid, entries, chosen, package, root)
    elif package is not None and package != appid:
        raise ForgeError("verification failed:\n    aapt2 reads the package of the universal APK as %s, the bundle says %s" % (package, appid))
    return {"apk": apk_path, "size": len(blob), "sha256": util.sha256_bytes(blob), "package": appid, "launchers": entries, "claims": claims}


def dexdump_classes(dexdump, dex):
    """The classes a dex file holds according to dexdump (a second, independent reader). None when dexdump could not read the file."""
    rc, out = run([dexdump, "-n", dex], check=False)
    if rc != 0 or "Opened '" not in out:
        return None
    return {m.group(1)[1:-1].replace("/", ".") for m in re.finditer(r"^\s*Class descriptor\s*:\s*'(L[^']*;)'", out, re.M)}


# ----------------------------------------------------------------------------------------------------------- builder
class Builder:
    def __init__(self, project, tc, release=False, offline=False, v4=None, optimize=None, react=None, sign_aab=True):
        self.p, self.tc, self.release = project, tc, release
        self.v4 = v4                       # None: each module's "v4Signing" setting; True / False: forced for this build (`build --v4` / `--no-v4`)
        self.variant = "release" if release else "debug"
        self.state = os.path.join(project.root, ".apkforge")
        os.makedirs(self.state, exist_ok=True)
        self.cache = Cache(os.path.join(self.state, "cache"), os.path.join(self.state, "last-build.json"))
        repos = [os.path.join(project.root, "vendor"), os.path.expanduser("~/.m2/repository")] + list(project.repositories) + deps.DEFAULT_REPOS
        self.vendor_dir = os.path.join(project.root, "vendor")
        self.tc.vendor_dir = self.vendor_dir
        self.mv = deps.Maven(repos, os.path.join(util.home_dir(), "m2"), offline, vendor_dir=self.vendor_dir)
        self.vendor_lock = dict(self.mv.lock)
        self.resolver = deps.Resolver(self.mv, os.path.join(util.home_dir(), "aar"), build_type=self.variant,
                                      java=max([int(m.java) for m in project.modules.values()] or [17]))
        self.built, self.outputs = {}, {}
        self.optimize = optimize           # None: each module's "optimizeResources" setting; True / False: forced for this build (`build --optimize-resources` / `--no-optimize-resources`)
        self.res_opt = {}                  # module name -> what the resource optimization did (set only when it ran)
        self.react = react                 # None: each module's "react" setting; True / False: forced for this build (`build --react` / `--no-react`)
        self.react_plans = {}              # module name -> react.Plan, or None (read once per build)
        self.js = {}                       # module name -> what the JavaScript bundle step did (set only when it ran)
        self.natives = {}                  # module name -> what native.build() made (None: no "native" setting); read once per build
        self.sign_aab = sign_aab           # False (`build --no-sign-aab`): the bundle is left unsigned, because it is signed somewhere else
        self.signers = {}                  # module name -> the Signer of the APK that was built (`test-bundle` signs its universal APK with it)

    # -------------------------------------------------------------------------------------------------- entry points
    @staticmethod
    def announce_zip_stamp():
        """A malformed SOURCE_DATE_EPOCH stops the build here, before any work is done; a valid one is mentioned once."""
        key = zipw.stamp_cache_key()
        if key:
            info("SOURCE_DATE_EPOCH=%s: zip entries are stamped %s UTC" % (os.environ[zipw.EPOCH_ENV].strip(), key))

    def build(self, name=None, aab=False):
        self.announce_zip_stamp()
        target = self.p.module(name) if name else self.p.default_app()
        res = None
        for m in topo(self.p, target.name):
            stage("%s (%s)" % (m.name, self.variant))
            res = self.build_app(m, aab) if m.is_app else self.build_library(m)
        self.cache.save()
        info("actions: %d cached, %d built" % (self.cache.stats["hit"], self.cache.stats["miss"]))
        return res

    def vendor(self, name=None):
        """Resolve the external dependencies of every module (or of one module and what it needs), then copy every file the
        resolution touched - POMs, parent POMs, BOMs, JARs, AARs - into <project>/vendor in Maven layout and record their
        sha256 in vendor/apkforge.lock.json. Builds look in vendor/ first and refuse a vendored file that no longer matches."""
        mods = topo(self.p, self.p.module(name).name) if name else list(self.p.modules.values())
        c = lambda lst: [deps.parse_coord(x) for x in lst]
        resolved = 0
        for m in mods:
            plat = c(m.deps["platforms"])
            android, plan = c(m.deps["api"] + m.deps["implementation"] + m.deps["runtimeOnly"] + m.deps["compileOnly"]), self.react_plan(m)
            if plan:                                                   # react-android and hermes-android at the versions react-native pins
                android = self.react_align(plan, android, [], [])[0]
            for target, coords in (("android", android),
                                   ("jvm", c(m.deps["annotationProcessor"] + m.deps["kapt"] + m.deps["ksp"]))):
                if coords:
                    self.resolver.resolve(coords, plat, target=target)
                    resolved += len(coords)
        if not resolved:
            raise ForgeError("nothing to vendor: no module declares external dependencies")
        if any(m.deps["ksp"] for m in mods):                   # the KSP2 runner is a build tool, but a ksp build cannot run without it: vendor it too
            r = self.tc.ksp_runner()
            if not r:
                raise ForgeError("a module uses ksp, but the KSP runner is not available (run `apkforge doctor --ksp`)")
            for n, jar in zip(toolchain.KSP_ARTIFACTS, r["jars"]):
                self.mv.used[deps.Maven.rel(toolchain.KSP_GROUP, n, r["version"], "jar")] = jar
            for pom in r["poms"]:
                n = os.path.basename(pom)[:-len("-%s.pom" % r["version"])]
                self.mv.used[deps.Maven.rel(toolchain.KSP_GROUP, n, r["version"], "pom")] = pom
        vdir, n, size = self.vendor_dir, 0, 0
        for rel, src in sorted(self.mv.used.items()):
            dst = os.path.join(vdir, rel.replace("/", os.sep))
            if os.path.abspath(src) != os.path.abspath(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
                n += 1
                size += os.path.getsize(dst)
        files = {}
        for p in util.tree_files(vdir):
            rel = os.path.relpath(p, vdir).replace(os.sep, "/")
            if rel != deps.LOCK_NAME and rel.endswith((".pom", ".jar", ".aar", ".module")):
                files[rel] = util.sha256_file(p)
        with open(os.path.join(vdir, deps.LOCK_NAME), "w") as f:
            json.dump({"note": "written by `apkforge vendor`; a build refuses a vendored file whose sha256 differs", "files": files}, f, indent=1, sort_keys=True)
        self.vendor_lock = self.mv.lock = files
        return n, size, vdir

    def work(self, m, *p):
        d = os.path.join(self.state, "build", m.name.lstrip(":").replace(":", "_"), self.variant, *p)
        os.makedirs(d, exist_ok=True)
        return d

    def aj(self, m):
        return self.tc.android_jar(m.compileSdk)

    # ---------------------------------------------------------------------------------------------- dependencies
    def prepare(self, m):
        pre = Prep()
        coords = lambda lst: [deps.parse_coord(x) for x in lst]
        plat = coords(m.deps["platforms"])
        cs, rs = coords(m.deps["api"] + m.deps["implementation"]), coords(m.deps["runtimeOnly"])
        co = coords(m.deps["compileOnly"])
        plan = self.react_plan(m)
        if plan:                                       # react-native pins react-android and hermes-android exactly; React Native's Gradle plugin forces those versions, here they are filled in
            cs, rs, co = self.react_align(plan, cs, rs, co)
        byname = {}
        if cs or rs or co:
            for a in self.resolver.resolve(cs + co + rs, plat):
                byname[a.name] = a
        pick = lambda cc: [byname["%s:%s" % (g, a)] for g, a, _v in cc]
        local = []
        libdir = os.path.join(m.dir, "libs")
        files = [os.path.join(m.dir, f) for f in m.files] + (sorted(glob.glob(os.path.join(libdir, "*.jar")) + glob.glob(os.path.join(libdir, "*.aar"))) if os.path.isdir(libdir) else [])
        for f in dict.fromkeys(files):
            if not os.path.isfile(f):
                raise ForgeError("%s: dependency file %s does not exist" % (m.name, f))
            local.append(deps.local_file(f, os.path.join(util.home_dir(), "aar")))
        projects = [self.built[self.p.module(d).name] for d in m.project_deps]
        api_roots = projects + pick(cs) + local
        rt_roots = api_roots + pick(rs)
        if m.uses_kotlin:
            kn = [a for a in deps.closure(rt_roots + pick(co)) if a.name.startswith("org.jetbrains.kotlin:kotlin-stdlib")]
            if not kn:
                extra = [deps.from_jar(self.tc.kotlin_jar(j), "kotlin-dist:" + j, "kotlin-dist/" + j) for j in ("kotlin-stdlib.jar", "annotations-13.0.jar") if self.tc.kotlin_jar(j)]
                api_roots, rt_roots = api_roots + extra, rt_roots + extra
        pre.features = self.features(m, {a.name for a in deps.closure(api_roots + pick(co) + pick(rs))})
        if pre.features.get("parcelize") and not any(a.name == "org.jetbrains.kotlin:kotlin-parcelize-runtime" for a in deps.closure(rt_roots + pick(co))):
            pj = self.tc.kotlin_jar("parcelize-runtime.jar")           # Gradle's kotlin-parcelize plugin adds this runtime; it ships inside the Kotlin distribution
            if not pj:
                raise ForgeError("%s: @Parcelize needs lib/parcelize-runtime.jar of the Kotlin distribution (not found)" % m.name)
            extra = [deps.from_jar(pj, "kotlin-dist:parcelize-runtime.jar", "kotlin-dist/parcelize-runtime.jar")]
            api_roots, rt_roots = api_roots + extra, rt_roots + extra
        if m.viewBinding and not any(a.name == "androidx.databinding:viewbinding" for a in deps.closure(api_roots + pick(co))):
            vb = [deps.from_jar(self.viewbinding_shim(m), "androidx.databinding:viewbinding", "androidx.databinding:viewbinding (apkforge shim)")]
            api_roots, rt_roots = api_roots + vb, rt_roots + vb
        pre.compile = deps.closure(api_roots + pick(co))
        pre.runtime = deps.closure(rt_roots)
        if plan:
            self.react_check_resolved(plan, pre)
        pre.direct_runtime = rt_roots
        compile_only = {a.name for a in pre.compile} - {a.name for a in pre.runtime}
        pre.compile_only = [a for a in pre.compile if a.name in compile_only]
        sk = {k: v for k, v in self.resolver.skipped.items() if k[0] + ":" + k[1] in byname}
        if sk and not getattr(self, "_skip_told", None) == sk:
            self._skip_told = dict(sk)
            warn("%s: %d version alignment(s) not applied because the aligned version is not in any repository (the vendor tree/kit has what a crawl asked for): %s%s" % (
                m.name, len(sk), ", ".join("%s:%s %s (using %s)" % (k[0].split(".")[-1], k[1], v[0], v[1]) for k, v in sorted(sk.items())[:6]),
                " ..." if len(sk) > 6 else ""))
        pre.processors, pre.kapt, pre.ksp = [], [], []
        for key, dst in (("annotationProcessor", pre.processors), ("kapt", pre.kapt), ("ksp", pre.ksp)):
            if m.deps[key]:                                    # processors run on a plain JVM: the jvm variants, not the Android ones
                for a in deps.closure(self.resolver.resolve(coords(m.deps[key]), plat, target="jvm")):
                    dst += [j for j in a.jars if j not in dst]
        return pre

    def viewbinding_shim(self, m):
        """androidx.viewbinding.ViewBinding + ViewBindings (what Gradle takes from androidx.databinding:viewbinding): compiled once, shared by all projects."""
        d = os.path.join(util.home_dir(), "shims")
        os.makedirs(d, exist_ok=True)
        jar = os.path.join(d, "viewbinding-%s.jar" % viewbinding.SHIM_VERSION)
        if not os.path.isfile(jar):
            tmp = tempfile.mkdtemp(prefix="vbshim-", dir=d)
            try:
                srcs = []
                for rel, text in viewbinding.SHIM_SOURCES.items():
                    p = os.path.join(tmp, "src", rel)
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    with open(p, "w") as f:
                        f.write(text)
                    srcs.append(p)
                out = os.path.join(tmp, "classes")
                os.makedirs(out)
                run([self.tc.ensure_javac(), "--release", "8", "-Xlint:-options", "-cp", self.aj(m), "-d", out] + srcs, what="javac (view binding shim)")
                jar_dirs([out], jar + ".tmp")
                os.replace(jar + ".tmp", jar)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        return jar

    def gen_view_binding(self, m, pre, ns, work):
        """-> directory with the generated <Layout>Binding.java files, or None."""
        if not m.viewBinding:
            return None
        out = os.path.join(work, "genbinding")
        shutil.rmtree(out, ignore_errors=True)
        ann = any(a.name in ("androidx.annotation:annotation", "androidx.annotation:annotation-jvm") for a in pre.compile)
        try:
            names = viewbinding.generate(m.res_dirs, ns, out, have_annotations=ann, warn=warn)
        except ValueError as e:
            raise ForgeError("%s: view binding: %s" % (m.name, e))
        if names:
            detail("view binding: %d class(es) in %s.databinding" % (len(names), ns))
        return out if names else None

    def ksp_lang(self, m):
        """-> (languageVersion, apiVersion) KSP analyses the sources with: the module's own -language-version / -api-version from kotlinArgs, else the
        major.minor of the Kotlin compiler that compiles them (so KSP and kotlinc read the sources the same way)."""
        args = [str(x) for x in m.kotlinArgs]

        def opt(name):
            for i, x in enumerate(args):
                if x == name and i + 1 < len(args):
                    return args[i + 1]
                if x.startswith(name + "="):
                    return x.split("=", 1)[1]
            return None
        kv = self.tc.kotlin_version(self.tc.kotlinc_home())
        mm = re.match(r"^(\d+\.\d+)", kv)
        lang = opt("-language-version") or (mm.group(1) if mm else "2.3")
        return lang, (opt("-api-version") or lang)

    def run_ksp(self, m, pre, cp, gen_dirs, kdir, resdir, label):
        """KSP = Kotlin Symbol Processing, run as KSP2's own command line (com.google.devtools.ksp.cmdline.KSPJvmMain): the processors of the `ksp`
        dependencies read the module's Kotlin and Java sources (the classpath `cp` is what they resolve types against) and write Kotlin / Java sources,
        resources and class files. -> the generated source files (.kt / .java); resources and class files go to resdir (they end up in classes.jar)."""
        r = self.tc.ksp_runner()
        if not r:
            raise ForgeError("%s: the `ksp` dependencies need the KSP runner (symbol-processing-aa-embeddable), which is not in vendor/ and could not be downloaded.\n"
                             "  `apkforge doctor --ksp` provisions it from the GitHub release; or put symbol-processing-aa-embeddable, -api and -common-deps (one version) "
                             "into <project>/vendor in Maven layout, or point APKFORGE_KSP at a folder that holds the three jars." % m.name)
        stdlib = self.tc.kotlin_jar("kotlin-stdlib.jar")
        if not stdlib:
            raise ForgeError("%s: ksp needs lib/kotlin-stdlib.jar of the Kotlin distribution (not found)" % m.name)
        d = {k: os.path.join(kdir, k) for k in ("kotlin", "java", "classes", "resources", "caches", "out")}
        for x in d.values():
            os.makedirs(x, exist_ok=True)
        ksp_args = expand_args(m, m.kspArgs)                                # BEFORE the check: the check must see the final text
        for k, v in ksp_args.items():
            if ":" in str(k) + str(v) or "=" in str(k):
                raise ForgeError("%s: kspArgs %r=%r cannot be passed on KSP's command line (':' separates the options, '=' the name from the value)" % (m.name, k, v))
        lang, api = self.ksp_lang(m)
        jvm = "1.8" if str(m.kotlinJvmTarget or m.java) == "8" else str(m.kotlinJvmTarget or m.java)
        jh = self.tc.jdk_home()
        java_bin = os.path.join(jh, "bin", util.exe("java")) if jh and os.path.isfile(os.path.join(jh, "bin", util.exe("java"))) else self.tc.java()
        args = ["-module-name=" + re.sub(r"\W", "_", label),
                "-source-roots=" + os.pathsep.join(m.src_dirs),
                "-java-source-roots=" + os.pathsep.join(list(m.src_dirs) + [g for g in gen_dirs if g and os.path.isdir(g)]),
                "-libraries=" + os.pathsep.join(cp),
                "-project-base-dir=" + m.dir, "-output-base-dir=" + d["out"], "-caches-dir=" + d["caches"],
                "-class-output-dir=" + d["classes"], "-kotlin-output-dir=" + d["kotlin"], "-java-output-dir=" + d["java"], "-resource-output-dir=" + d["resources"],
                "-language-version=" + lang, "-api-version=" + api, "-jvm-target=" + jvm]
        if jh:
            args.append("-jdk-home=" + jh)
        if ksp_args:
            args.append("-processor-options=" + ":".join("%s=%s" % (k, v) for k, v in ksp_args.items()))
        args.append(os.pathsep.join(pre.ksp))                          # the processor classpath is the one positional argument
        cmd = [java_bin] + self.tc.jvm_flags(2048) + ["-Xss16m", "-cp", os.pathsep.join(r["jars"] + [stdlib]), "com.google.devtools.ksp.cmdline.KSPJvmMain"] + args
        _rc, out = run(cmd, what="ksp %s (%s)" % (r["version"], label))
        for line in out.splitlines():                                  # the processors' own warnings are the user's to see
            if line.startswith(("w: ", "warning: ")):
                warn("ksp (%s): %s" % (label, line.split(": ", 1)[1]))
        shutil.rmtree(d["caches"], ignore_errors=True)
        for root in (d["classes"], d["resources"]):
            for p in util.tree_files(root):
                dst = os.path.join(resdir, os.path.relpath(p, root))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(p, dst)
        return sorted(util.tree_files(d["kotlin"], (".kt",)) + util.tree_files(d["java"], (".java",)))

    def run_kapt(self, m, pre, kt, java, cp, kdir, resdir, label):
        """kapt = the Kotlin compiler's annotation-processing plugin: it makes Java stubs of the Kotlin sources, runs the processors over stubs + Java
        sources, and writes what they generate. -> the generated source files (.java / .kt); non-class resources go to resdir."""
        home = self.tc.kotlinc_home()
        plug = os.path.join(home, "lib", "kotlin-annotation-processing.jar")
        if not os.path.isfile(plug):
            raise ForgeError("%s: kapt needs lib/kotlin-annotation-processing.jar of the Kotlin distribution (not found in %s)" % (m.name, home))
        dirs = {k: os.path.join(kdir, k) for k in ("src", "classes", "stubs", "incr", "out")}
        for d in dirs.values():
            os.makedirs(d, exist_ok=True)
        opts = []
        for k, v in (("sources", dirs["src"]), ("classes", dirs["classes"]), ("stubs", dirs["stubs"]), ("incrementalData", dirs["incr"]),
                     ("aptMode", "stubsAndApt"), ("correctErrorTypes", "true"), ("useLightAnalysis", "true"), ("mapDiagnosticLocations", "true")):
            opts += ["-P", "plugin:org.jetbrains.kotlin.kapt3:%s=%s" % (k, v)]
        for j in pre.kapt:
            opts += ["-P", "plugin:org.jetbrains.kotlin.kapt3:apclasspath=" + j]
        if m.kaptArgs:
            opts += ["-P", "plugin:org.jetbrains.kotlin.kapt3:apoptions=" + kapt_encode(expand_args(m, m.kaptArgs))]
        jvm = "1.8" if str(m.kotlinJvmTarget or m.java) == "8" else str(m.kotlinJvmTarget or m.java)
        env = {"JAVA_OPTS": " ".join(self.tc.jvm_flags(1536) + KAPT_ADD_OPENS)}
        jh = self.tc.jdk_home()
        if jh:
            env["JAVA_HOME"] = jh
        plugins = [p for p in self.kotlin_plugins(m, pre) if "compose" not in p]
        cmd = [os.path.join(home, "bin", "kotlinc"), "-no-stdlib", "-no-reflect", "-jvm-target", jvm, "-module-name", re.sub(r"\W", "_", label),
               "-d", dirs["out"], "-cp", os.pathsep.join(cp), "-Xplugin=" + plug] + plugins + opts + list(m.kotlinArgs) + kt + java
        run(cmd, env=env, what="kapt (%s)" % label)
        gen = util.tree_files(dirs["src"], (".java", ".kt"))
        for p in util.tree_files(dirs["classes"]):
            if not p.endswith(".class"):                      # resources the processors wrote (META-INF/services/..., schemas)
                dst = os.path.join(resdir, os.path.relpath(p, dirs["classes"]))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(p, dst)
        return sorted(gen)

    def features(self, m, names):
        """Which Kotlin compiler plugins this module compiles with. An explicit true/false in apkforge.json wins; otherwise the classpath decides
        (names = every g:a the module compiles against)."""
        def auto(v, test):
            return bool(test) if v is None or v == "auto" else bool(v)
        if not m.uses_kotlin:
            return {"compose": False, "serialization": False, "parcelize": False}
        return {
            "compose": auto(m.compose, any(n.startswith(("androidx.compose.runtime:runtime", "org.jetbrains.compose.runtime:runtime")) for n in names)),
            "serialization": auto(m.serialization, any(n.startswith("org.jetbrains.kotlinx:kotlinx-serialization-core") for n in names)),
            "parcelize": auto(m.parcelize, self._sources_mention(m, (b"kotlinx.parcelize", b"kotlinx.android.parcel"))),
        }

    @staticmethod
    def _sources_mention(m, needles):
        for f in m.sources(".kt"):
            with open(f, "rb") as fh:
                data = fh.read()
            if any(n in data for n in needles):
                return True
        return False

    def kotlin_plugins(self, m, pre):
        """-Xplugin arguments for the compiler plugins that ship in the Kotlin distribution (lib/)."""
        home, out = self.tc.kotlinc_home(), []
        for feat, jar in (("compose", "compose-compiler-plugin.jar"), ("serialization", "kotlinx-serialization-compiler-plugin.jar"),
                          ("parcelize", "parcelize-compiler.jar")):
            if (pre.features or {}).get(feat):
                p = os.path.join(home, "lib", jar)
                if not os.path.isfile(p):
                    raise ForgeError("%s: %s needs lib/%s, which this Kotlin distribution does not have" % (m.name, feat, jar))
                out.append("-Xplugin=" + p)
        return out

    # ---------------------------------------------------------------------------------------------------- manifest
    def make_manifest(self, m, pre, ns, appid):
        text = open(m.manifest, encoding="utf-8").read() if m.manifest else None
        if text is None:
            if m.is_app:
                raise ForgeError("%s: no AndroidManifest.xml (looked in src/main/ and the module root)" % m.name)
            text = '<manifest xmlns:android="%s" package="%s"/>' % (M.ANDROID, ns)
        root = M.parse(text, m.manifest or "AndroidManifest.xml")
        root.set("package", ns)
        M.expand_names(root, ns)
        if m.is_app:
            libs = [(M.parse(open(a.manifest, encoding="utf-8").read(), a.manifest), a.package or "", a.label) for a in pre.runtime if a.manifest]
            warnings = []
            M.merge(root, libs, m.minSdk, warnings)
            for w in warnings:
                warn(w)
        M.set_sdk(root, m.minSdk, m.targetSdk)
        app_el = root.find("application")
        if m.is_app and app_el is not None:
            if not self.release:
                app_el.set(M.A + "debuggable", "true")
            if any(a.jni for a in pre.runtime) or m.jni_dirs:
                if m.minSdk >= 23 and (M.A + "extractNativeLibs") not in app_el.attrib:
                    app_el.set(M.A + "extractNativeLibs", "false")
        M.apply_tools(root)
        ph = {"applicationId": appid if m.is_app else ns}
        ph.update({k: str(v) for k, v in m.placeholders.items()})
        text = M.substitute(M.to_text(root), ph)
        return text, M.parse(text, "merged manifest")

    # -------------------------------------------------------------------------------------------------- resources
    def res_source_base(self, res_dir):
        """-> (base, rel): aapt2 records the --dir argument as the source path of every compiled resource and, for an AAB (--proto-format), writes it
        into base/resources.pb. Compiling `rel` from `base` instead of the absolute path keeps the bundle independent of where the project sits (F1).
        base is the project root, or apkforge's own cache (dependency resources); (None, res_dir) for anything else: the path stays absolute."""
        real = os.path.realpath(res_dir)
        for base in (os.path.realpath(self.p.root), os.path.realpath(util.home_dir())):
            try:
                inside = os.path.commonpath([base, real]) == base
            except ValueError:                                    # another drive on Windows
                inside = False
            if inside and real != base:
                return base, os.path.relpath(real, base).replace(os.sep, "/")
        return None, res_dir

    def compile_res(self, res_dir, label):
        files = util.tree_files(res_dir)
        if not files:
            return None
        aapt2 = self.tc.aapt2()
        base, rel = self.res_source_base(res_dir)
        # `src` is part of the key because the compiled files carry it: a module that moved must be compiled again
        with self.cache.action("aapt2-compile:" + label, {"res": util.tree_hash(res_dir), "aapt2": self.tc.ident("aapt2"), "src": rel}) as a:
            if not a.hit:
                run([aapt2, "compile", "--dir", rel, "-o", a.path("res.zip")], cwd=base, what="aapt2 compile (%s)" % label)
        return a.path("res.zip")

    def overlay_zips(self, m, pre, arts, work, final):
        """The compiled resource overlays of one aapt2 link, in the order of its -R flags (a later one wins): the resources of every library in `arts` (lowest priority first), a React Native app's Metro
        drawables, the module's own res folders, and last the folder that `resValues` become (written to work/gen_res). The APK link and the bundle link both take this list: they used to build it twice,
        and the bundle's copy had no gen_res, so a resource made by resValues was in the APK and missing from the bundle."""
        overlays = []
        for a in arts:
            for rd in a.res:
                z = self.compile_res(rd, a.label + ":" + os.path.basename(os.path.dirname(rd) or rd))
                if z:
                    overlays.append(z)
        gen_vals = gen_res_values(m)
        gen_res = None
        if gen_vals:
            gen_res = os.path.join(work, "gen_res")
            os.makedirs(os.path.join(gen_res, "values"), exist_ok=True)
            with open(os.path.join(gen_res, "values", "generated.xml"), "w") as f:
                f.write(gen_vals)
        js = getattr(pre, "js", None) if final else None
        if js and os.path.isdir(js["res"]):               # Metro's drawables and raw files: below the module's own resources, as for the plugin's generated res folder
            z = self.compile_res(js["res"], "%s:%s:react-res" % (m.name, self.variant))
            if z:
                overlays.append(z)
        for rd in list(m.res_dirs) + ([gen_res] if gen_res else []):
            z = self.compile_res(rd, "%s:%s:%s" % (m.name, self.variant, os.path.basename(rd)))
            if z:
                overlays.append(z)
        return overlays

    def link_resources(self, m, pre, ns, appid, manifest_text, work, final):
        """final=True: the app link (final ids, R classes for every library package, keep rules).
        final=False: a library's own link, only to get R.java with non-final ids for compiling it."""
        mf = os.path.join(work, "AndroidManifest.xml")
        with open(mf, "w", encoding="utf-8") as f:
            f.write(manifest_text)
        arts = list(reversed(pre.runtime if final else deps.closure(pre.direct_runtime)))
        overlays = self.overlay_zips(m, pre, arts, work, final)
        js = getattr(pre, "js", None) if final else None
        assets = None
        if final:
            ad = [d for a in arts for d in a.assets] + list(m.asset_dirs)
            if js:
                ad = self.react_assets(m, js, ad)
            if ad:
                assets = os.path.join(work, "assets")
                shutil.rmtree(assets, ignore_errors=True)
                for d in ad:
                    shutil.copytree(d, assets, dirs_exist_ok=True)
        extra = sorted({a.package for a in arts if a.package and a.package != ns})
        inputs = {"manifest": util.sha256_bytes(manifest_text.encode()), "overlays": [util.sha256_file(z) for z in overlays],
                  "assets": util.tree_hash(assets) if assets else None, "aapt2": self.tc.ident("aapt2"), "extra": extra, "final": final,
                  "sdk": [m.minSdk, m.targetSdk, m.compileSdk], "ver": [m.versionCode, m.versionName] if final else None, "appid": appid, "ns": ns}
        compact = final and self.compact_entries(m)
        if compact:
            inputs["compact"] = True                      # only set when on: the key (and the cache) of every other build stays what it was
        with self.cache.action("%s:%s:link-%s" % (m.name, self.variant, "app" if final else "lib"), inputs) as a:
            if not a.hit:
                gen = a.path("gen")
                os.makedirs(gen)
                cmd = [self.tc.aapt2(), "link", "-I", self.aj(m), "--manifest", mf, "-o", a.path("base.apk"), "--min-sdk-version", str(m.minSdk),
                       "--target-sdk-version", str(m.targetSdk), "--auto-add-overlay", "--java", gen, "--custom-package", ns,
                       "--output-text-symbols", a.path("R.txt")]
                if final:
                    cmd += ["--version-code", str(m.versionCode), "--version-name", str(m.versionName), "--proguard", a.path("aapt.pro")]
                    if appid != ns:
                        cmd += ["--rename-manifest-package", appid, "--rename-instrumentation-target-package", appid]
                else:
                    cmd += ["--non-final-ids"]
                if extra:
                    cmd += ["--extra-packages", ":".join(extra)]
                if compact:
                    cmd += ["--enable-compact-entries"]
                for z in overlays:
                    cmd += ["-R", z]
                if assets:
                    cmd += ["-A", assets]
                run(cmd, what="aapt2 link (%s)" % m.name)
        return {"apk": a.path("base.apk"), "gen": a.path("gen"), "rtxt": a.path("R.txt"), "pro": a.path("aapt.pro"), "res_overlays": overlays}

    # ------------------------------------------------------------------------------------------------------ compile
    def compile_sources(self, m, pre, gen_dirs, label, strip_r=()):
        """-> classes.jar of the module's own classes (or None when it has no sources)."""
        java = m.sources(".java") + [f for d in gen_dirs for f in util.tree_files(d, (".java",))]
        kt = m.sources(".kt")
        if not java and not kt:
            return None
        aj = self.aj(m)
        jars = [aj] + [j for a in pre.compile for j in a.jars]
        inputs = {"src": util.tree_hash(m.src_dirs), "gen": util.tree_hash(gen_dirs), "cp": [a.fingerprint() for a in pre.compile],
                  "aj": util.sha256_file(aj)[:16], "javac": self.tc.ident("javac"), "java": m.java, "jargs": m.javacArgs,
                  "kt": self.tc.ident("kotlinc") if kt else None, "kargs": m.kotlinArgs, "proc": [util.sha256_file(p)[:12] for p in pre.processors], "kapt": [util.sha256_file(p)[:12] for p in pre.kapt], "kaptargs": m.kaptArgs,
                  "ksp": [util.sha256_file(p)[:12] for p in pre.ksp], "kspargs": m.kspArgs, "kspv": self.tc.ident("ksp") if (pre.ksp and kt) else None,
                  "vb": [util.tree_hash(m.res_dirs), viewbinding.SHIM_VERSION] if m.viewBinding else None,
                  "variant": self.variant, "api": m.apiCheck, "strip": list(strip_r), "ktjvm": m.kotlinJvmTarget,
                  "kplug": sorted(k for k, v in (pre.features or {}).items() if v) if kt else None}
        lib_res = [] if m.is_app else list(m.resource_dirs)       # an app packages these itself; a library hands them on inside classes.jar
        if lib_res:
            inputs["javares"] = util.tree_hash(lib_res)
        with self.cache.action("%s:%s:compile" % (label, self.variant), inputs) as a:
            if not a.hit:
                out_kt, out_java, apt = a.path("kt"), a.path("java"), a.path("apt")
                for d in (out_kt, out_java, apt):
                    os.makedirs(d)
                cp = list(jars)
                procs, kres = list(pre.processors), None
                if (pre.ksp or pre.kapt) and kt:
                    kres = a.path("genres")                      # resources / class files the processors wrote
                    os.makedirs(kres)
                if pre.ksp:
                    if kt:                                       # KSP first, as in Gradle: what it generates is compiled with (and seen by) kapt and kotlinc
                        gen_src = self.run_ksp(m, pre, cp, gen_dirs, a.path("ksp"), kres, label)
                        java = java + [f for f in gen_src if f.endswith(".java")]
                        kt = kt + [f for f in gen_src if f.endswith(".kt")]
                    else:
                        warn("%s has `ksp` dependencies but no Kotlin sources: KSP only runs with the Kotlin compiler (as in Gradle), so they are ignored" % label)
                if pre.kapt:
                    if kt:
                        gen_src = self.run_kapt(m, pre, kt, java, cp, a.path("kapt"), kres, label)
                        java = java + [f for f in gen_src if f.endswith(".java")]
                        kt = kt + [f for f in gen_src if f.endswith(".kt")]
                    else:                                       # no Kotlin: a kapt dependency is plain javac annotation processing
                        procs += [j for j in pre.kapt if j not in procs]
                if kt:
                    self.run_kotlinc(m, kt, java, cp, out_kt, label, pre)
                    cp = [out_kt] + cp
                if java:
                    args = ["--release", str(m.java), "-encoding", "UTF-8", "-Xlint:-options", "-d", out_java, "-cp", os.pathsep.join(cp), "-s", apt]
                    args += (["-processorpath", os.pathsep.join(procs)] if procs else ["-proc:none"])
                    args += (["-g"] if not self.release else []) + list(m.javacArgs) + java
                    argf = a.path("javac.args")
                    with open(argf, "w") as f:
                        f.write("\n".join('"%s"' % x.replace("\\", "\\\\").replace('"', '\\"') for x in args))
                    run([self.tc.ensure_javac(), "-J-XX:+UseSerialGC", "-J-XX:TieredStopAtLevel=1", "-J-Xmx1g", "@" + argf], what="javac (%s)" % label)
                pk = [re.escape(p.replace(".", "/")) for p in strip_r]
                rx = re.compile(r"^(%s)/R(\$[^/]*)?\.class$" % "|".join(pk)) if pk else None
                stripped = set()               # compiled against, then left out of the jar (library R classes): still valid references
                if rx:
                    for d in (out_kt, out_java):
                        for p in util.tree_files(d):
                            rel = os.path.relpath(p, d).replace(os.sep, "/")
                            if rel.endswith(".class") and rx.match(rel):
                                stripped.add(rel[:-6])
                jar_dirs([out_kt, out_java] + ([kres] if kres else []) + lib_res, a.path("classes.jar"), exclude=(lambda r: bool(rx.match(r))) if rx else None)
                notes = self.api_guard(m, pre, a.path("classes.jar"), stripped)
                with open(a.path("guard.txt"), "w") as f:
                    f.write("\n".join(notes))
        for line in open(a.path("guard.txt")).read().splitlines():
            warn(line)
        self.min_api_guard(m, pre, a.path("classes.jar"), label)
        return a.path("classes.jar")

    def run_kotlinc(self, m, kt, java, cp, out, label, pre=None):
        home = self.tc.kotlinc_home()
        if not home:
            raise ForgeError("%s has .kt sources but no Kotlin compiler is available (network, or set KOTLIN_HOME)" % label)
        jvm = "1.8" if str(m.kotlinJvmTarget or m.java) == "8" else str(m.kotlinJvmTarget or m.java)
        jh = self.tc.jdk_home()
        env = {"JAVA_OPTS": " ".join(self.tc.jvm_flags(1536))}
        if jh:
            env["JAVA_HOME"] = jh
        base = [os.path.join(home, "bin", "kotlinc"), "-no-stdlib", "-no-reflect", "-jvm-target", jvm, "-module-name", re.sub(r"\W", "_", label),
                "-d", out, "-cp", os.pathsep.join(cp)] + (self.kotlin_plugins(m, pre) if pre is not None else []) + list(m.kotlinArgs)
        try:                                 # -no-jdk: android.jar is then the only platform API, so JDK-only APIs fail at compile time
            run(base + ["-no-jdk"] + kt + java, env=env, what="kotlinc (%s)" % label)
        except ForgeError as e:
            if "unresolved reference" in str(e).lower() or "unresolved" in str(e):
                raise
            warn("kotlinc with -no-jdk failed; retrying with the JDK on the classpath (API guard still applies)")
            run(base + kt + java, env=env, what="kotlinc (%s)" % label)

    def api_guard(self, m, pre, classes_jar, known=()):
        if m.apiCheck == "off":
            return []
        api = classfile.Api(self.aj(m))
        others = set(known)
        for a in pre.compile:
            for j in a.jars:
                others |= set(classfile.jar_class_names(j))
        errors, _newer = classfile.check_api(jar_classes(classes_jar), api, others)
        if errors:
            msg = "API guard: %d reference(s) to APIs that android.jar %d does not have (they would crash on a device):\n    %s" % (
                len(errors), m.compileSdk, "\n    ".join(errors[:12]) + ("\n    ... %d more" % (len(errors) - 12) if len(errors) > 12 else ""))
            if m.apiCheck == "error":
                raise ForgeError(msg + "\n  (set \"apiCheck\": \"warn\" in apkforge.json to downgrade this)")
            return [msg]
        return []

    def min_api_guard(self, m, pre, classes_jar, label):
        """minApiCheck: platform APIs newer than minSdk that no SDK_INT check, @RequiresApi or @TargetApi protects.
        It runs in its OWN cache action, keyed by what it reads and not by the mode. A verdict that could not be completed (offline, or no android.jar
        for some level) is shown but never stored, so it cannot be replayed after the network is back. warn / error apply on every run, replays included.
        `error` fails only on references the check could verify: a check that could not run is reported, it does not fail the build."""
        mode = m.minApiCheck
        if mode == "off":
            return
        try:
            lo, hi = int(m.minSdk), int(m.compileSdk)
        except (TypeError, ValueError):
            warn("min-API guard skipped: minSdk %r and compileSdk %r must be API level numbers" % (m.minSdk, m.compileSdk))
            return
        if lo >= hi:
            return                                   # nothing in the compile-time android.jar is newer than minSdk
        jars = [j for a in pre.compile for j in a.jars]
        key = {"v": apiguard.VERSION, "classes": util.sha256_file(classes_jar), "min": lo, "compile": hi, "aj": util.sha256_file(self.aj(m))[:16],
               "cp": [a.fingerprint() for a in pre.compile], "dexer": self.tc.ident("dexer")}
        rendered = None
        try:
            with self.cache.action("%s:%s:minapi" % (label, self.variant), key) as act:
                if act.hit:
                    with open(act.path("result.json")) as f:
                        rendered = json.load(f)
                else:
                    rendered, complete = self._min_api_run(m, jars, classes_jar, lo, hi)
                    with open(act.path("result.json"), "w") as f:
                        json.dump(rendered, f)
                    if not complete:
                        raise _Incomplete()          # Action.__exit__ deletes its tmp dir when the body raises: nothing is stored
        except _Incomplete:
            pass
        newer, unver, notes = rendered["newer"], rendered["unverified"], rendered["notes"]
        if newer and mode == "error":
            raise ForgeError("\n".join([newer] + ([unver] if unver else []) + notes))
        for text in [newer, unver] + notes:
            if text:
                warn(text)

    def _min_api_run(self, m, jars, classes_jar, lo, hi):
        """-> (the rendered messages as a JSON-able dict, whether the check could be completed)"""
        tc = self.tc

        def jar_for(level):
            try:
                return classfile.Api(tc.android_jar_exact(level, hi))
            except ForgeError as e:
                raise apiguard.Unavailable(str(e).splitlines()[0].split(". ")[0])
        others = set()
        for j in jars:
            others |= set(classfile.jar_class_names(j))
        res = apiguard.check(jar_classes(classes_jar), classfile.Api(self.aj(m)), lo, hi, jar_for,
                             deps=apiguard.JarIndex(jars) if jars else None, backports=tc.backported_methods, others=others)
        newer, unver, notes = apiguard.render(res, lo, None)
        return {"newer": newer, "unverified": unver, "notes": notes}, not (res.skipped or res.skipped_levels)

    # ------------------------------------------------------------------------------------------------------ library
    def build_library(self, m):
        pre = self.prepare(m)
        pre.native = self.native_build(m)
        ns = m.namespace
        text0 = open(m.manifest, encoding="utf-8").read() if m.manifest else ""
        if not ns and text0:
            ns = M.package_of(M.parse(text0, m.manifest))
        if not ns:
            raise ForgeError("%s: set \"namespace\" in apkforge.json (or a package attribute in its manifest)" % m.name)
        m.namespace = ns
        work = self.work(m)
        mtext, _ = self.make_manifest(m, pre, ns, ns)
        lib_mf = os.path.join(work, "lib-manifest.xml")
        with open(lib_mf, "w", encoding="utf-8") as f:
            f.write(mtext)
        r = self.link_resources(m, pre, ns, ns, mtext, work, final=False)
        gens = [r["gen"]]
        vbd = self.gen_view_binding(m, pre, ns, work)
        if vbd:
            gens.append(vbd)
        jar = self.compile_sources(m, pre, gens, m.name, strip_r=sorted({ns} | {a.package for a in deps.closure(pre.direct_runtime) if a.package}))
        art = deps.Artifact(m.name, "project " + m.name, ns)
        art.module, art.manifest = m, lib_mf
        art.jars = [jar] if jar else []
        art.res, art.assets, art.jni = list(m.res_dirs), list(m.asset_dirs), ([pre.native["dir"]] if pre.native else []) + list(m.jni_dirs)       # the AAR's jni/ carries what CMake built, first
        art.proguard = [os.path.join(m.dir, f) for f in m.cfg.get("consumerProguardFiles", []) if os.path.isfile(os.path.join(m.dir, f))]
        art.deps = list(pre.direct_runtime)
        art.rtxt = r["rtxt"]
        self.built[m.name] = art
        aar = self.make_aar(m, art, work)
        self.outputs[m.name] = aar
        ok("%s -> %s (%s)" % (m.name, os.path.relpath(aar, self.p.root), util.human(os.path.getsize(aar))))
        res = {"aar": aar}
        if pre.native:
            res["native"] = native.summary(pre.native)
        return res

    def make_aar(self, m, art, work):
        zb = zipw.ZipBuilder()
        zb.add("AndroidManifest.xml", open(art.manifest, "rb").read())
        zb.add("classes.jar", open(art.jars[0], "rb").read() if art.jars else _empty_jar())
        zb.add("R.txt", open(art.rtxt, "rb").read())
        for sub, dirs in (("res", art.res), ("assets", art.assets), ("jni", art.jni)):
            for d in dirs:
                for p in util.tree_files(d):
                    zb.add(sub + "/" + os.path.relpath(p, d).replace(os.sep, "/"), open(p, "rb").read())
        for p in art.proguard:
            zb.add("proguard.txt", open(p, "rb").read())
        out = os.path.join(work, "%s-%s.aar" % (m.name.lstrip(":").replace(":", "-"), self.variant))
        with open(out, "wb") as f:
            f.write(zb.finish())
        return out

    def publish_local(self, name, repo, group=None, version=None):
        """Write a library module into a Maven-layout directory (usable as a repository by other projects)."""
        m = self.p.module(name)
        if m.is_app:
            raise ForgeError("%s is an app, not a library" % m.name)
        self.announce_zip_stamp()
        for d in topo(self.p, m.name):
            if not d.is_app and d.name not in self.built:
                self.build_library(d)
        aar = self.outputs[m.name]
        g = group or m.cfg.get("group") or m.namespace
        a = m.name.lstrip(":").replace(":", "-")
        v = version or m.cfg.get("version") or str(m.versionName)
        base = os.path.join(repo, g.replace(".", "/"), a, v)
        os.makedirs(base, exist_ok=True)
        shutil.copyfile(aar, os.path.join(base, "%s-%s.aar" % (a, v)))
        dep_xml = ""
        for scope, lst in (("compile", m.deps["api"] + m.deps["implementation"]), ("runtime", m.deps["runtimeOnly"])):
            for c in lst:
                dg, da, dv = deps.parse_coord(c)
                dep_xml += "    <dependency><groupId>%s</groupId><artifactId>%s</artifactId>%s<scope>%s</scope></dependency>\n" % (
                    dg, da, "<version>%s</version>" % dv if dv else "", scope)
        for pd in m.project_deps:
            pm = self.p.module(pd)
            dep_xml += "    <dependency><groupId>%s</groupId><artifactId>%s</artifactId><version>%s</version><scope>compile</scope></dependency>\n" % (
                pm.cfg.get("group") or pm.namespace or g, pm.name.lstrip(":").replace(":", "-"), pm.cfg.get("version") or pm.versionName)
        dm = "".join("      <dependency><groupId>%s</groupId><artifactId>%s</artifactId><version>%s</version><type>pom</type><scope>import</scope></dependency>\n" % deps.parse_coord(c)
                     for c in m.deps["platforms"])
        pom = ('<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">\n  <modelVersion>4.0.0</modelVersion>\n'
               '  <groupId>%s</groupId>\n  <artifactId>%s</artifactId>\n  <version>%s</version>\n  <packaging>aar</packaging>\n' % (g, a, v))
        if dm:
            pom += "  <dependencyManagement>\n    <dependencies>\n%s    </dependencies>\n  </dependencyManagement>\n" % dm
        pom += "  <dependencies>\n%s  </dependencies>\n</project>\n" % dep_xml
        with open(os.path.join(base, "%s-%s.pom" % (a, v)), "w") as f:
            f.write(pom)
        ok("published %s:%s:%s -> %s" % (g, a, v, base))
        return "%s:%s:%s" % (g, a, v)

    # ----------------------------------------------------------------------------------------------------------- app
    def build_app(self, m, aab=False):
        pre = self.prepare(m)
        pre.native = self.native_build(m)                          # CMake + the NDK (modules with a "native" setting only): the libraries join lib/<abi>/ of the APK and the AAB
        text0 = open(m.manifest, encoding="utf-8").read() if m.manifest else ""
        ns = m.namespace or (M.package_of(M.parse(text0, m.manifest)) if text0 else None)
        if not ns:
            raise ForgeError("%s: set \"namespace\" (or \"applicationId\") in apkforge.json" % m.name)
        appid = m.application_id or ns
        work = self.work(m)
        pre.js = self.react_bundle(m, pre, appid)                  # Metro + hermesc (React Native modules only); before the link, which takes its assets and resources
        mtext, mroot = self.make_manifest(m, pre, ns, appid)
        r = self.link_resources(m, pre, ns, appid, mtext, work, final=True)
        gens = [r["gen"]]
        vbd = self.gen_view_binding(m, pre, ns, work)
        if vbd:
            gens.append(vbd)
        src_text = "".join(open(f, encoding="utf-8", errors="replace").read() for f in m.sources(".java") + m.sources(".kt"))
        if m.buildConfig is True or (m.buildConfig is None and "BuildConfig" in src_text):
            bc = os.path.join(work, "genjava")
            shutil.rmtree(bc, ignore_errors=True)
            os.makedirs(os.path.join(bc, *ns.split(".")))
            with open(os.path.join(bc, *ns.split("."), "BuildConfig.java"), "w") as f:
                f.write(gen_buildconfig(m, ns, appid, self.release))
            gens.append(bc)
        jar = self.compile_sources(m, pre, gens, m.name)
        dex_dir = self.dex(m, pre, jar, r["pro"])
        out_dir = os.path.join(self.p.root, "build", "outputs")
        os.makedirs(out_dir, exist_ok=True)
        base_apk = self.optimize_resources(m, pre, r, jar, appid, out_dir)          # aapt2 optimize between link and assemble (opt-in, release only)
        apk_unsigned = self.assemble(m, pre, base_apk, dex_dir, work)
        signer = self.signer_for(m)
        self.signers[m.name] = signer
        apk = os.path.join(out_dir, "%s-%s.apk" % (appid, self.variant))
        blob = signing.sign_zip(apk_unsigned, signer, v1=bool(m.minSdk < 24 or m.cfg.get("v1Signing")), v2=True, v3=True)
        idsig = write_apk(apk, blob, signer, self.v4_on(m))          # v4 covers every byte of the file: nothing may touch the APK from here on
        report = self.verify(m, apk, mroot, dex_dir, signer, appid)
        result = {"apk": apk, "size": len(blob), "sha256": util.sha256_bytes(blob), "report": report,
                  "zip_stamp": zipw.stamp_label(zipw.stamp_from_env()) + " UTC",
                  "application_id": appid, "namespace": ns, "launchers": M.launchers(mroot)}       # what `apkforge run` starts: the id and class names differ when applicationId != namespace
        if idsig:
            result["idsig"] = idsig
        if self.res_opt.get(m.name):
            result["resources"] = self.res_opt[m.name]                 # only an optimized build carries this key: the result of any other build keeps its shape
        if self.js.get(m.name):
            result["js"] = self.js[m.name]["result"]                   # likewise: only a build that bundled JavaScript
        if pre.native:
            result["native"] = native.summary(pre.native)             # likewise: only a build that compiled native code
        if aab:
            result["aab"], result["aab_report"] = self.make_aab(m, pre, ns, appid, mtext, work, dex_dir, out_dir, link=r, own_jar=jar)
        self.outputs[m.name] = apk
        ok("APK  %s  (%s)  sha256 %s" % (os.path.relpath(apk, self.p.root), util.human(len(blob)), result["sha256"][:16]))
        if idsig:
            ok("v4   %s  (%s)  sha256 %s" % (os.path.relpath(idsig, self.p.root), util.human(os.path.getsize(idsig)), util.sha256_file(idsig)[:16]))
        return result

    # ------------------------------------------------------------------------------------------------ NDK / CMake (native.py)
    def native_build(self, m):
        """-> what native.build() made for m ({"dir": <abi>/<lib>.so tree, "libs", ...}), or None when m has no "native" setting.  Once per build."""
        nats = self.__dict__.setdefault("natives", {})
        if m.name not in nats:
            opts = native.parse_option(m.cfg.get("native"), 'apkforge.json (module %s): "native"' % m.name)
            nats[m.name] = native.build(self.tc, opts, m.dir, self.p.root, self.work(m, "native"), self.release, list(m.abis), m.minSdk, m.name) if opts else None
        return nats[m.name]

    def native_claim(self, m, apk):
        """verify's claim about the libraries CMake built: every one of them is in the finished APK, byte for byte (nothing replaced it, nothing shadowed it)."""
        nat = self.natives[m.name]
        want = {"lib/%s/%s" % (l["abi"], l["name"]): l["sha256"] for l in nat["libs"]}
        with zipfile.ZipFile(apk) as z:
            names = set(z.namelist())
            have = {n: util.sha256_bytes(z.read(n)) for n in want if n in names}
        gone = sorted(n for n in want if n not in have)
        if gone:
            raise ForgeError("verification failed:\n    native librar%s built by CMake but not in the APK: %s" % ("y" if len(gone) == 1 else "ies", ", ".join(gone[:6])))
        other = sorted(n for n in want if have[n] != want[n])
        if other:
            raise ForgeError("verification failed:\n    the APK holds other bytes than CMake built for: %s" % ", ".join(other[:6]))
        n = len(want)
        text = "%d native librar%s built from CMake (NDK %s; %s)" % (n, "y" if n == 1 else "ies", nat["ndk"]["revision"], ", ".join(nat["abis"]))
        return _claim("native-libs", str(n), text, "internal", ("internal",))

    # ------------------------------------------------------------------------------------------------ React Native / Hermes (react.py)
    def react_option(self, m):
        """-> react.Options when this build bundles m's JavaScript, else None.  Opt in with "react" or `build --react` (which wins both ways); apps only."""
        want = react.parse_option(m.cfg.get("react"), 'apkforge.json (module %s): "react"' % m.name)
        if self.react is not None:
            want = (want or react.parse_option(True)) if self.react else None
        return want if m.is_app else None

    def react_plan(self, m):
        """-> the react.Plan of a React Native module (node_modules read once per build; what follows from them alone is enforced here), else None."""
        if m.name not in self.react_plans:
            opts, plan = self.react_option(m), None
            if opts:
                plan = react.make_plan(m.dir, opts)
                errs, warns = react.static_findings(plan)
                react.enforce(plan, errs, warns)
            self.react_plans[m.name] = plan
        return self.react_plans[m.name]

    def react_align(self, plan, cs, rs, co):
        """The module's coordinate lists with react-native's pins applied (react.align_coordinates): a versionless react-android / hermes-android gets the pinned version, the
        plugin's substitutions are made, and a pinned library the module does not name joins `cs`.  -> (cs, rs, co)."""
        allc = cs + rs + co
        aligned, notes = react.align_coordinates(plan, allc)
        for n in notes:
            info("react: " + n)
        k = len(cs)
        return aligned[:k] + aligned[len(allc):], aligned[k:k + len(rs)], aligned[k + len(rs):len(allc)]

    def react_check_resolved(self, plan, pre):
        """After the resolution: the react-android and hermes-android that were RESOLVED must be the pinned ones (a dependency may ask for another)."""
        got = {a.name: a.version for a in pre.runtime}
        versions = {"react-android": got.get("%s:react-android" % plan.react_group), "hermes-android": got.get("%s:hermes-android" % plan.hermes_group)}
        errs, warns = react.resolved_findings(plan, versions)
        react.enforce(plan, errs, warns)

    def react_assets(self, m, js, ad):
        """The asset folders plus the one Metro filled.  A file of the bundle's name in the app's own assets would clash with it: an error, not a silent winner."""
        name = js["meta"]["name"]
        for d in ad:
            if os.path.exists(os.path.join(d, name)):
                raise ForgeError("%s: %s already holds %s, the file the JavaScript bundle is written to.  Remove it, or set \"bundleAssetName\" in the \"react\" section" % (m.name, d, name))
        return list(ad) + [js["assets"]]

    def react_bundle(self, m, pre, appid):
        """Metro, hermesc and the source map composition (react.build_bundle) as one cached action -> {"plan", "meta", "assets", "res", "maps", "result"}, or None when the module is
        no React Native app or this variant is debuggable (the plugin does not bundle those either: the app loads its JavaScript from Metro)."""
        plan = self.react_plans.get(m.name)
        if not plan:
            return None
        o = plan.opts
        if self.variant in o.debuggable_variants:
            info("react: a %s build carries no JavaScript bundle (React Native's plugin skips debuggable variants too; the app loads it from Metro).  --release, or "
                 "\"debuggableVariants\": [] in the \"react\" section, bundles it" % self.variant)
            return None
        with self.cache.action("%s:%s:react-bundle" % (m.name, self.variant), react.cache_inputs(plan)) as a:
            if not a.hit:
                react.build_bundle(plan, a.dir)
        meta = react.read_meta(a.path())
        maps = os.path.join(self.p.root, "build", "outputs", "%s-%s-sourcemaps" % (appid, self.variant))
        shutil.rmtree(maps, ignore_errors=True)
        os.makedirs(maps)
        for n in meta["maps"]:
            shutil.copyfile(a.path("maps", n), os.path.join(maps, n))
        ok("JavaScript: " + react.summary(meta))
        result = {"asset": "assets/" + meta["name"], "size": meta["bundle"]["size"], "sha256": meta["bundle"]["sha256"], "hermes": bool(meta["hermes"]),
                  "sourcemaps": maps, "versions": meta["versions"]}
        if meta.get("hbc"):
            result["bytecode_version"] = meta["hbc"]["version"]
        js = {"plan": plan, "meta": meta, "assets": a.path("assets"), "res": a.path("res"), "maps": maps, "result": result}
        self.js[m.name] = js
        return js

    def js_claim(self, m, apk):
        """verify's claim about the JavaScript in the finished APK: the bundle is there, is the one that was built, is stored where the plugin keeps it stored, and (Hermes) its
        bytecode header names the version that hermesc itself reports it writes."""
        js = self.js[m.name]
        plan, meta = js["plan"], js["meta"]
        name = meta["name"]
        facts = react.apk_bundle_facts(apk, name)
        if not facts["present"]:
            raise ForgeError("verification failed:\n    assets/%s is not in the APK" % name)
        if facts["sha256"] != meta["bundle"]["sha256"]:
            raise ForgeError("verification failed:\n    assets/%s in the APK is not the bundle that was built (sha256 differs)" % name)
        if react.nocompress_ext(plan.opts) and not facts["stored"]:
            raise ForgeError("verification failed:\n    assets/%s is compressed in the APK (React Native's plugin keeps it stored, so that Hermes reads the bytecode in place)" % name)
        how = "stored" if facts["stored"] else "compressed"
        if not meta.get("hbc"):
            return _claim("js-bundle", name, "JavaScript assets/%s: plain JavaScript (minified, no Hermes), %s, %s" % (name, util.human(facts["size"]), how), "internal", ("internal",))
        head = facts["header"]
        if not head["magic"] or head["version"] != meta["hbc"]["version"]:
            raise ForgeError("verification failed:\n    assets/%s in the APK does not start with the Hermes bytecode that hermesc wrote (header read: %s)" % (name, json.dumps(head)))
        hv = react.hermesc_version(plan.hermesc)
        if hv["bytecode"] != head["version"]:
            raise ForgeError("verification failed:\n    assets/%s is Hermes bytecode v%d, but `hermesc -version` says that compiler writes v%d" % (name, head["version"], hv["bytecode"]))
        text = "JavaScript assets/%s: Hermes bytecode v%d, %s, %s" % (name, head["version"], util.human(facts["size"]), how)
        if plan.custom_hermesc:
            return _claim("js-bundle", name, text, "internal", ("internal",), "your own hermesc: its version is not checked against the pinned runtime %s" % plan.hermes_runtime)
        if plan.compiler_version != plan.hermes_runtime:
            return _claim("js-bundle", name, text, "internal", ("internal",), "hermes-compiler %s is not the runtime %s that react-native pins (pinCheck allowed it)" % (plan.compiler_version, plan.hermes_runtime))
        return _claim("js-bundle", name, text, "confirmed", ("internal", "hermesc"))

    # ---------------------------------------------------------------------------------------- resource optimization
    def optimize_option(self, m):
        """-> resopt.Options when this build optimizes m's resources, else None.  Opt in with "optimizeResources" or `build --optimize-resources` (which wins both ways)."""
        want = resopt.parse_option(m.cfg.get("optimizeResources"), 'apkforge.json (module %s): "optimizeResources"' % m.name)
        if self.optimize is not None:
            want = (want or resopt.parse_option(True)) if self.optimize else None
        return want

    def compact_entries(self, m):
        """Does the app link ask for compact resource entries?  Opt in with "compactEntries"; aapt2 applies them from minSdk 34, and `aapt2 optimize` writes the plain
        layout again, so an optimized build does not ask for them."""
        if not m.cfg.get("compactEntries"):
            return False
        if m.minSdk < resopt.COMPACT_MIN_SDK:
            warn('"compactEntries" needs minSdk %d or higher (this app: %d): aapt2 would ignore it' % (resopt.COMPACT_MIN_SDK, m.minSdk))
            return False
        if self.release and self.optimize_option(m):
            warn('"compactEntries" is undone by aapt2 optimize (it writes the plain layout again): not asked for in this build')
            return False
        return True

    def optimize_resources(self, m, pre, r, own_jar, appid, out_dir):
        """aapt2 optimize between `link` and `assemble`.  -> the resources APK that assemble() reads: the link's own unless this build optimizes.
        Release builds only (a debug build keeps its names and paths for debugging).  A cached action; its result is read back with arsc.py every time."""
        want = self.optimize_option(m)
        stem = os.path.join(out_dir, "%s-%s%s" % (appid, self.variant, resopt.MAP_SUFFIX))
        for ext in (".txt", ".pb"):                    # a map that lies beside a build which did not write it would describe another build
            try:
                os.remove(stem + ext)
            except OSError:
                pass
        if want is None:
            return r["apk"]
        if not self.release:
            (warn if self.optimize else info)("optimizeResources: release builds only (this is a debug build, which keeps its names and paths for debugging); add --release")
            return r["apk"]
        aapt2 = self.tc.aapt2()
        _, help_text = run([aapt2, "optimize", "-h"], check=False, what="aapt2 optimize -h")
        with zipfile.ZipFile(r["apk"]) as z:
            before = arsc.parse(z.read("resources.arsc"))
        p = resopt.plan(want, before, m.minSdk, help_text)
        for note in p.notes:
            info(note)
        if p.noop:
            info("optimizeResources: nothing to do with these settings")
            return r["apk"]
        if p.collapse:
            self.guard_collapse(m, pre, own_jar, before, p)
        inputs = {"apk": util.sha256_file(r["apk"]), "aapt2": self.tc.ident("aapt2"), "flags": p.flags, "cfg": util.sha256_bytes(p.cfg.encode()) if p.cfg else None}
        with self.cache.action("%s:%s:optimize" % (m.name, self.variant), inputs) as a:
            if not a.hit:
                if p.cfg:
                    with open(a.path("resources.cfg"), "w", encoding="utf-8", newline="\n") as f:
                        f.write(p.cfg)
                run(p.command(aapt2, r["apk"], a.path("optimized.apk"), a.path("resources.cfg"), a.path("map.pb")), what="aapt2 optimize (%s)" % m.name)
        out = a.path("optimized.apk")
        map_blob = open(a.path("map.pb"), "rb").read() if os.path.isfile(a.path("map.pb")) else b""
        rep = resopt.check(r["apk"], out, p, map_blob)
        rep["flags"] = list(p.flags)
        if p.wants_map:
            with open(stem + ".txt", "w", encoding="utf-8", newline="\n") as f:
                f.write(resopt.render_map(resopt.decode_map(map_blob) if map_blob else {"paths": {}, "names": {}}, before.named_ids(), appid, self.variant))
            if map_blob:
                with open(stem + ".pb", "wb") as f:
                    f.write(map_blob)
            rep["map"] = os.path.relpath(stem + ".txt", self.p.root).replace(os.sep, "/")

        def pct(a_, b_):
            return "%+.1f%%" % (100.0 * (b_ - a_) / a_) if a_ else "n/a"
        ok("resources optimized (%s): resources.arsc %s -> %s bytes (%s); resources APK %s -> %s bytes (%s); %d path(s) shortened, %d name(s) collapsed%s%s; "
           "ids and values checked against the unoptimized table" % (
               " ".join(f.lstrip("-") for f in p.flags), format(rep["arsc_before"], ","), format(rep["arsc_after"], ","), pct(rep["arsc_before"], rep["arsc_after"]),
               format(rep["apk_before"], ","), format(rep["apk_after"], ","), pct(rep["apk_before"], rep["apk_after"]), rep["paths_shortened"], rep["names_collapsed"],
               " (%d kept)" % len(p.keep) if p.collapse else "", ", %d sparse type chunk(s)" % rep["sparse_chunks"] if rep["sparse_chunks"] else ""))
        if rep.get("map"):
            info("map of the renamed paths and names: %s (keep it with the build)" % rep["map"])
        self.res_opt[m.name] = rep
        return out

    def guard_collapse(self, m, pre, own_jar, table, p):
        """Collapsing breaks Resources.getIdentifier(name, ...).  Scan the classes that go into the APK (cached by their hashes); stop when the code calls it AND holds
        the name of a resource that is not in "keep"."""
        jars = list(dict.fromkeys(([own_jar] if own_jar else []) + [j for a in pre.runtime for j in a.jars]))
        inputs = {"jars": [util.sha256_file(j) for j in jars], "names": util.sha256_bytes("\n".join(sorted(resopt.table_names(table))).encode()), "keep": p.keep}
        with self.cache.action("%s:%s:res-guard" % (m.name, self.variant), inputs) as a:
            if not a.hit:
                strings, uses = resopt.scan_jars(jars)
                errors, warnings = resopt.guard(strings, uses, table, p.keep)
                with open(a.path("result.json"), "w") as f:
                    json.dump({"errors": errors, "warnings": warnings}, f)
        with open(a.path("result.json")) as f:
            res = json.load(f)
        for w in res["warnings"]:
            warn("optimizeResources: " + w)
        if res["errors"]:
            raise ForgeError("optimizeResources: " + "; ".join(res["errors"]))

    def resource_claim(self, apk):
        """verify's claim about the optimized APK's resource table: apkforge's reader and aapt2 (`dump resources`) list the same ids, and every file the table names is in the APK"""
        ids, refs, missing, entries = resopt.final_table(apk)
        if missing:
            raise ForgeError("verification failed:\n    the resource table points at file(s) that are not in the APK: " + ", ".join(missing[:5]))
        rc, out = run([self.tc.aapt2(), "dump", "resources", apk], check=False, what="aapt2 dump resources")
        if rc != 0:
            raise ForgeError("verification failed:\n    aapt2 cannot read the resource table of the optimized APK: " + ((out.strip().splitlines() or ["no output"])[-1])[:200])
        theirs = resopt.dump_ids(out)
        if theirs != ids:
            raise ForgeError("verification failed:\n    aapt2 lists %d resource ids in the optimized APK, apkforge's own reader finds %d" % (len(theirs), len(ids)))
        return _claim("resource-table", str(len(ids)), "resource table: %d resource ids agree, all %d files it names are present" % (len(ids), len(refs)), "confirmed", ("internal", "aapt2"))

    def v4_on(self, m):
        """Does this build write `<apk>.idsig`?  `build --v4` / `--no-v4` decides for the whole build, otherwise the module's "v4Signing" (off unless set)."""
        return bool(m.cfg.get("v4Signing")) if self.v4 is None else bool(self.v4)

    # --------------------------------------------------------------------------------------------------------- dex
    def dex(self, m, pre, own_jar, aapt_rules):
        jars = ([own_jar] if own_jar else []) + [j for a in pre.runtime for j in a.jars]
        jars = list(dict.fromkeys(jars))
        if not jars:
            return None
        aj = self.aj(m)
        minify = bool(self.release and m.minify)
        rules = ""
        if minify:
            rules = DEFAULT_R8_RULES + "\n" + open(aapt_rules).read() + "\n"
            for a in pre.runtime:
                for p in a.proguard:
                    rules += "\n# consumer rules of %s\n%s\n" % (a.label, open(p, encoding="utf-8", errors="replace").read())
            for f in m.cfg.get("proguardFiles", []):
                p = os.path.join(m.dir, f)
                if os.path.isfile(p):
                    rules += "\n# %s\n%s\n" % (f, open(p, encoding="utf-8", errors="replace").read())
        inputs = {"jars": [util.sha256_file(j) for j in jars], "min": m.minSdk, "release": self.release, "minify": minify,
                  "rules": util.sha256_bytes(rules.encode()), "d8": self.tc.ident("dexer"), "aj": util.sha256_file(aj)[:16],
                  "co": [a.fingerprint() for a in pre.compile_only], "r8args": m.cfg.get("r8Args", [])}
        with self.cache.action("%s:%s:%s" % (m.name, self.variant, "r8" if minify else "d8"), inputs) as a:
            if not a.hit:
                out = a.path("dex")
                os.makedirs(out)
                cmd = [self.tc.java()] + self.tc.jvm_flags(2048 if minify else 1536) + ["-cp", self.tc.dexer(),
                       "com.android.tools.r8.%s" % ("R8" if minify else "D8"), "--release" if self.release else "--debug", "--min-api", str(m.minSdk),
                       "--lib", aj, "--output", out]
                for c in pre.compile_only:
                    for j in c.jars:
                        cmd += ["--classpath", j]
                if minify:
                    pgf = a.path("rules.pro")
                    with open(pgf, "w") as f:
                        f.write(rules)
                    cmd += ["--pg-conf", pgf, "--pg-map-output", a.path("mapping.txt")] + list(m.cfg.get("r8Args", []))
                _, o = run(cmd + jars, what="%s (%s)" % ("R8" if minify else "D8", m.name))
                if "defined multiple times" in o:
                    raise ForgeError("duplicate classes between dependencies:\n  " + "\n  ".join(l for l in o.splitlines() if "defined multiple times" in l)[:600])
        if minify:
            md = os.path.join(self.p.root, "build", "outputs", "mapping")
            os.makedirs(md, exist_ok=True)
            shutil.copyfile(a.path("mapping.txt"), os.path.join(md, "%s-mapping.txt" % m.name.lstrip(":")))
        return a.path("dex")

    # ------------------------------------------------------------------------------------------------------ package
    def java_resources(self, m, pre):
        """The Java resources (not Android resources) an app carries: non-class files of the runtime jars, the module's own
        src/main/resources, and META-INF/services files MERGED across all of them. The APK and the AAB both use this one
        function, so they cannot drift apart. -> ordered {name: bytes}; the first definition of a name wins."""
        out, services = {}, {}

        def svc(n, data):
            services.setdefault(n, []).extend(data.decode("utf-8", "replace").splitlines())
        own = []
        for d in m.resource_dirs:
            for p in util.tree_files(d):
                own.append((os.path.relpath(p, d).replace(os.sep, "/"), p))
        for n, p in own:                                  # the app's own providers are listed first
            if n.startswith("META-INF/services/"):
                svc(n, open(p, "rb").read())
        for jp in [j for a in pre.runtime for j in a.jars]:
            with zipfile.ZipFile(jp) as z:
                for n in z.namelist():
                    if n.endswith("/") or n.endswith(".class") or n.endswith(".java") or SIG_FILE.match(n) or n.startswith("META-INF/versions/") or n == "module-info.class":
                        continue
                    if n.startswith("META-INF/services/"):
                        svc(n, z.read(n))
                    elif not LICENSE_FILE.match(n):
                        out.setdefault(n, z.read(n))
        for n, p in own:
            if not n.startswith("META-INF/services/"):
                out.setdefault(n, open(p, "rb").read())
        for n in sorted(services):
            lines = [l for l in dict.fromkeys(x.strip() for x in services[n]) if l and not l.startswith("#")]
            out.setdefault(n, ("\n".join(lines) + "\n").encode())
        return out

    def assemble(self, m, pre, base_apk, dex_dir, work):
        nocomp = NO_COMPRESS | {x.lower().lstrip(".") for x in m.noCompress}
        js = getattr(pre, "js", None)
        if js and react.nocompress_ext(js["plan"].opts):            # React Native's plugin adds the bundle's extension to noCompress: the bytecode is read in place
            nocomp = nocomp | {react.nocompress_ext(js["plan"].opts)}
        zb = zipw.ZipBuilder()
        names = set()

        def add(n, data, store=None, align=0):
            if n in names:
                return
            names.add(n)
            ext = n.rsplit(".", 1)[-1].lower() if "." in n.rsplit("/", 1)[-1] else ""
            st = (ext in nocomp) if store is None else store
            zb.add(n, data, store=st, align=align if st else 0)
        with zipfile.ZipFile(base_apk) as z:
            add("AndroidManifest.xml", z.read("AndroidManifest.xml"))
            if dex_dir:
                dexes = sorted(os.listdir(dex_dir), key=lambda s: (len(s), s))
                for d in dexes:
                    if d.endswith(".dex"):
                        add(d, open(os.path.join(dex_dir, d), "rb").read(), store=False)
            for n in z.namelist():
                if n == "resources.arsc":
                    add(n, z.read(n), store=True, align=4)
                elif n != "AndroidManifest.xml" and not n.endswith("/"):
                    add(n, z.read(n))
        abis = set(m.abis)
        nat = getattr(pre, "native", None)
        jni = ([nat["dir"]] if nat else []) + [d for a in pre.runtime for d in a.jni] + list(m.jni_dirs)       # the module's own CMake libraries first: they win a duplicate path
        for d in jni:
            for p in util.tree_files(d):
                rel = os.path.relpath(p, d).replace(os.sep, "/")
                parts = rel.split("/")
                if len(parts) == 2 and parts[0] in abis and parts[1].endswith(".so"):
                    add("lib/" + rel, open(p, "rb").read(), store=True, align=zipw.PAGE_16K)
        for n, data in self.java_resources(m, pre).items():
            add(n, data)
        return zb

    def keystore_signer_for(self, m, section):
        """The Signer of the keystore that the `section` section of apkforge.json names: "signing", or "uploadSigning" for the App Bundle."""
        sc = m.cfg[section]
        if not sc.get("keystore"):
            raise ForgeError('apkforge.json: "%s" needs "keystore"' % section)
        ks = sc["keystore"] if os.path.isabs(sc["keystore"]) else os.path.join(m.dir, sc["keystore"])
        if sc.get("storePassword") or sc.get("keyPassword"):
            warn("%s: a password is written in apkforge.json in plain text; use storePasswordEnv / keyPasswordEnv and export the variable, "
                 "so that no password is ever committed" % section)
        sp = sc.get("storePassword") or os.environ.get(sc.get("storePasswordEnv", "APKFORGE_STORE_PASS"))
        kp = sc.get("keyPassword") or os.environ.get(sc.get("keyPasswordEnv", "APKFORGE_KEY_PASS")) or sp
        if not sp:
            raise ForgeError("release signing: no store password (set %s.storePasswordEnv in apkforge.json and export that variable)" % section)
        return signing.keystore_signer(ks, sc.get("alias"), sp, kp, keytool=self.tc.keytool())

    def signer_for(self, m):
        sc = m.cfg.get("signing")
        appid = str(m.application_id or "")
        if self.release and (appid == "com.example" or appid.startswith("com.example.")):
            warn("applicationId %s: Google Play does not accept packages that start with com.example; set \"applicationId\" in apkforge.json "
                 "(the code namespace may stay as it is)" % appid)
        if self.release and sc:
            return self.keystore_signer_for(m, "signing")
        if self.release:
            warn("release build signed with the DEBUG key: add a \"signing\" section to apkforge.json before publishing (apkforge keygen does both: the keystore and the section)")
        return signing.debug_signer(util.home_dir())

    def aab_signer(self, m):
        """-> (Signer, where it comes from: "uploadSigning" | "signing" | "debug").  A release build signs the bundle with the "uploadSigning" key when the module has that section, else with
        its "signing" key (one key for the APK and the bundle), else with the debug key (verify_bundle then says Google Play refuses it).  A debug build always uses the debug key, as it does for the APK."""
        if self.release:
            for section in ("uploadSigning", "signing"):
                if m.cfg.get(section):
                    return self.keystore_signer_for(m, section), section
        return signing.debug_signer(util.home_dir()), "debug"

    # ---------------------------------------------------------------------------------------------------- verify
    def verify(self, m, apk, mroot, dex_dir, signer, appid):
        rep = signing.verify_apk(apk)
        if rep["problems"]:
            raise ForgeError("signature self-check failed: " + "; ".join(rep["problems"]))
        problems = []
        ns = M.package_of(mroot) or appid
        declared = M.components(mroot, ns)
        have = set()
        dexes = [os.path.join(dex_dir, f) for f in sorted(os.listdir(dex_dir)) if f.endswith(".dex")] if dex_dir else []
        for p in dexes:
            with open(p, "rb") as f:
                have |= {c[1:-1].replace("/", ".") for c in classfile.dex_classes(f.read())}
        missing = sorted(c for c in declared if c not in have)
        if missing:
            problems.append("manifest declares component(s) with no class in the dex: " + ", ".join(missing))
        exported = 0
        for el in mroot.iter():
            if el.tag in M.COMPONENTS and el.find("intent-filter") is not None and m.targetSdk >= 31:
                exported += 1
                if (M.A + "exported") not in el.attrib:
                    problems.append("%s %s has an intent-filter but no android:exported (required for targetSdk >= 31; the install is rejected)" % (el.tag, el.get(M.A + "name")))
        if problems:
            raise ForgeError("verification failed:\n    " + "\n    ".join(problems))
        found = self.tc.oracles()
        ora = run_apk_oracles(self.tc, apk, m.minSdk, m.cfg.get("verifyWithApksigner", True), found)
        for reason in oracle_rejections(ora):
            raise ForgeError(reason)
        # dexdump: a second reader of the dex files. It may fail to PARSE (a dex version it does not know): that only downgrades the claim.
        dd = found.get("dexdump")
        dd_ok, dd_why = False, ("dexdump not found" if not dd else "no dex file to read")
        if dd and dexes:
            dd_have, dd_why = set(), ""
            for p in dexes:
                cl = dexdump_classes(dd, p)
                if cl is None:
                    dd_why = "dexdump could not read the dex"
                    break
                dd_have |= cl
            if not dd_why:
                dd_ok = True
                dd_missing = sorted(c for c in declared if c not in dd_have)
                if dd_missing:
                    raise ForgeError("verification failed:\n    dexdump finds no class for manifest component(s) that apkforge's own dex reader found: " + ", ".join(dd_missing))
        claims = signature_claims(rep, ora) + [alignment_claim(ora)]
        elf = {"libs": [], "claims": [], "rule": [], "errors": [], "warnings": []}
        if m.cfg.get("elfAlignment", "warn") != "off":
            elf = elf_report(found, apk)
            claims += apply_elf_policy(m.cfg.get("elfAlignment", "warn"), elf, apk)
        if (getattr(self, "natives", None) or {}).get(m.name):
            claims.append(self.native_claim(m, apk))
        if self.res_opt.get(m.name):
            claims.append(self.resource_claim(apk))
        if self.js.get(m.name):
            claims.append(self.js_claim(m, apk))
        n = len(declared)
        text = "%d manifest component%s in dex" % (n, "" if n == 1 else "s")
        claims.append(_claim("manifest-components", str(n), text, "confirmed", ("internal", "dexdump")) if dd_ok else _claim("manifest-components", str(n), text, "internal", ("internal",), dd_why))
        if exported:
            claims.append(_claim("exported-attribute", str(exported), "android:exported declared on %d intent-filter component%s" % (exported, "" if exported == 1 else "s"), "internal", ("internal",)))
        ok("verified: " + render_claims(claims))
        oracles = [name for name, st in (("apksigner", ora["apksigner"]["state"]), ("zipalign", ora["zipalign"]["state"]), ("dexdump", "ok" if dd_ok else "")) if st == "ok"]
        if any("readelf" in c["basis"] for c in claims):
            oracles.append("readelf")
        sig = {k: rep[k] for k in ("v1", "v2", "v3")}
        if rep.get("v4") is not None:                  # only when a .idsig lies beside the APK: the report of a build without v4 keeps its shape
            sig["v4"] = rep["v4"]
        report = {"signature": sig, "components": sorted(declared), "oracles": oracles, "claims": claims}
        if elf["libs"]:
            report["native_libs"] = elfcheck.strip(elf["libs"])
        return report

    # ------------------------------------------------------------------------------------------------------- AAB
    def make_aab(self, m, pre, ns, appid, mtext, work, dex_dir, out_dir, link=None, own_jar=None):
        """Android App Bundle: aapt2 --proto-format link -> base module zip -> bundletool build-bundle (with the bundle config of "optimizeResources") -> signed -> checked.
        link = what link_resources() returned, own_jar = the module's compiled classes (the checks of a collapsing "optimizeResources").  -> (path of the bundle, report: see finish_aab)."""
        bt = self.tc.bundletool()
        if not bt:
            raise ForgeError("bundletool is not available (needs network once, or set APKFORGE_BUNDLETOOL)")
        pw = os.path.join(work, "aab")
        shutil.rmtree(pw, ignore_errors=True)
        os.makedirs(pw)
        proto = os.path.join(pw, "proto.apk")
        mf = os.path.join(pw, "AndroidManifest.xml")
        open(mf, "w", encoding="utf-8").write(mtext)
        arts = list(reversed(pre.runtime))
        cmd = [self.tc.aapt2(), "link", "--proto-format", "-I", self.aj(m), "--manifest", mf, "-o", proto, "--min-sdk-version", str(m.minSdk),
               "--target-sdk-version", str(m.targetSdk), "--auto-add-overlay", "--custom-package", ns, "--version-code", str(m.versionCode),
               "--version-name", str(m.versionName)]
        if appid != ns:
            cmd += ["--rename-manifest-package", appid]
        for z in self.overlay_zips(m, pre, arts, work, True):                            # the bundle is always the app link: the very list the APK was linked with
            cmd += ["-R", z]
        js = getattr(pre, "js", None)
        ad = [d for a in arts for d in a.assets] + list(m.asset_dirs)
        if js:
            ad = self.react_assets(m, js, ad)
        if ad:
            merged = os.path.join(pw, "assets")
            for d in ad:
                shutil.copytree(d, merged, dirs_exist_ok=True)
            cmd += ["-A", merged]
        run(cmd, what="aapt2 link --proto-format")
        base = zipfile.ZipFile(os.path.join(pw, "base.zip"), "w", zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(proto) as z:
            for n in z.namelist():
                if n == "AndroidManifest.xml":
                    base.writestr("manifest/AndroidManifest.xml", z.read(n))
                elif n == "resources.pb":
                    base.writestr("resources.pb", z.read(n))
                elif not n.endswith("/"):
                    base.writestr(n, z.read(n))      # res/**, assets/**
        for d in (sorted(os.listdir(dex_dir)) if dex_dir else []):
            if d.endswith(".dex"):
                base.write(os.path.join(dex_dir, d), "dex/" + d)
        libs = set()                             # a library that two dependencies ship is written ONCE, as assemble() does for the APK (the first copy wins; pre.runtime is highest priority first, `arts` is reversed for the resource overlay): react-android and fbjni both carry libc++_shared.so and libfbjni.so, and zipfile would write both, which bundletool refuses ("Multiple entries with same key")
        nat = getattr(pre, "native", None)
        for d in ([nat["dir"]] if nat else []) + [x for a in pre.runtime for x in a.jni] + list(m.jni_dirs):
            for p in util.tree_files(d):
                rel = os.path.relpath(p, d).replace(os.sep, "/")
                if rel.split("/")[0] in m.abis and rel.endswith(".so") and rel not in libs:
                    libs.add(rel)
                    base.write(p, "lib/" + rel)
        skipped = []
        for n, data in self.java_resources(m, pre).items():          # same set as the APK: services, kotlin_module, own resources ...
            if n.split("/", 1)[0] in AAB_RESERVED:
                skipped.append(n)
            else:
                base.writestr("root/" + n, data)
        if skipped:
            warn("%d Java resource(s) not copied into the AAB (their first path component is reserved for bundletool): %s" % (len(skipped), ", ".join(skipped[:5])))
        base.close()
        out = os.path.join(out_dir, "%s-%s.aab" % (appid, self.variant))
        if os.path.exists(out):
            os.remove(out)
        cmd = [self.tc.java()] + self.tc.jvm_flags(1024) + ["-jar", bt, "build-bundle", "--modules=" + os.path.join(pw, "base.zip"), "--output=" + out]
        cfg, notes = aabmod.bundle_config(self.bundle_resource_options(m, pre, link, own_jar), react.nocompress_ext(js["plan"].opts) if js else None)
        for n in notes:
            info(n)
        if cfg:                                  # optimizeResources; and React Native's plugin adds the bundle's extension to noCompress and AGP hands that to bundletool: the bytecode stays STORED in the APKs made from this AAB
            cfg_file = os.path.join(pw, "BundleConfig.json")
            with open(cfg_file, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=1)
            cmd.append("--config=" + cfg_file)
            info("bundle config: " + aabmod.describe_config(cfg))
        run(cmd, what="bundletool build-bundle")
        return out, self.finish_aab(m, out)

    def bundle_resource_options(self, m, pre, link, own_jar):
        """-> the resopt.Options of "optimizeResources" that go into the bundle's config, or None.  Release builds only, like the APK (the APK step already said so for a debug build).
        What apkforge can know before bundletool runs is checked here, as for the APK: a "keep" name must be a resource, and the code must not look up by name (Resources.getIdentifier)
        a resource that collapsing would take away."""
        want = self.optimize_option(m)
        if want is None or not self.release:
            return None
        if want.collapse and link:
            with zipfile.ZipFile(link["apk"]) as z:
                table = arsc.parse(z.read("resources.arsc"))
            resopt.check_keep(list(want.keep), table)
            self.guard_collapse(m, pre, own_jar, table, resopt.Plan([], "", want.keep, [], True, False))
        return want

    def finish_aab(self, m, out):
        """Sign the bundle bundletool just wrote (not with `build --no-sign-aab`), read it back with every reader that is available (verify_bundle) and say what Google Play will object to.
        -> the report that goes into the build result as "aab_report": {path, size, sha256, signed_with, signature, bundle, play, oracles, claims}.  ForgeError when a reader rejects the bundle."""
        signed_with = None
        if self.sign_aab:
            signer, signed_with = self.aab_signer(m)
            signing.sign_aab(out, out, signer)
        check = verify_bundle(self.tc, out, unsigned_ok=not self.sign_aab)
        if check["problems"]:
            raise ForgeError("verification failed (%s):\n    %s" % (os.path.relpath(out, self.p.root), "\n    ".join(check["problems"])))
        size, sha = os.path.getsize(out), util.sha256_file(out)
        how = {"uploadSigning": 'signed with the upload key ("uploadSigning")', "signing": 'signed with the release key ("signing")', "debug": "signed with the DEBUG key",
               None: "UNSIGNED (--no-sign-aab): sign it before you upload it"}[signed_with]
        ok("AAB  %s  (%s)  sha256 %s  %s" % (os.path.relpath(out, self.p.root), util.human(size), sha[:16], how))
        ok("verified: " + render_claims(check["claims"]))
        for w in check["signature"]["warnings"]:
            warn("bundle signature: " + w)
        say = warn if self.release else info                # a debug bundle is not meant for Google Play: say it, but do not nag
        for e in check["play"]["errors"]:
            say("Google Play would refuse this bundle: " + e)
        for w in check["play"]["warnings"]:
            if not (self.release and w.startswith("applicationId")):          # signer_for said that already
                say("Google Play: " + w)
        return {"path": out, "size": size, "sha256": sha, "signed_with": signed_with,
                "signature": {k: check["signature"][k] for k in ("signed", "warnings", "signers")},
                "bundle": check["bundle"], "play": check["play"], "oracles": check["oracles"], "claims": check["claims"]}

    def clean(self):
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(os.path.join(self.p.root, "build"), ignore_errors=True)


def _empty_jar():
    zb = zipw.ZipBuilder()
    zb.add("META-INF/", b"")
    return zb.finish()
