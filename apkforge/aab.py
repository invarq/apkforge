"""Android App Bundles: read one, check it for Google Play, try it out on this machine.

There is no protobuf library here.  A bundle holds two protobuf files that matter, `BundleConfig.pb` and the proto manifest
`<module>/manifest/AndroidManifest.xml` (aapt2's XmlNode), and both are read with the small wire reader below.  What the reader finds is checked against
bundletool's own `dump manifest` / `dump config` whenever bundletool is installed (cross_check): a wrong field number shows up as a disagreement, never as
a quietly wrong answer.

  read_bundle(path)                 what the bundle holds: package, version, SDK levels, debuggable, launchers, modules, config   (no JDK, no network)
  play_findings(bundle, sig)        what Google Play will object to, as errors and warnings
  run_bundle_oracles(tc, path)      the external readers that are installed: jarsigner -verify, bundletool validate / dump manifest / dump config
  cross_check(bundle, root, ora)    do apkforge's decoders and bundletool's dumps say the same thing?
  bundle_config(opt, ext)           the BundleConfig.json that carries optimizeResources (and React's uncompressed glob) into `bundletool build-bundle`
  universal_apk(tc, aab, ...)       `bundletool build-apks --mode=universal`, repacked and signed the way apkforge signs APKs: an APK that installs, for a test
"""
import io
import json
import os
import xml.etree.ElementTree as ET
import zipfile

from . import manifest as M
from . import signing, util, zipw
from .util import ForgeError, run

PLAY_CERT_UNTIL = "2033-10-22"          # Google Play's signing documentation: the signing key must have a validity period that ends after 22 October 2033
READ_CAP = 16 << 20                     # a manifest or BundleConfig.pb of more than 16 MB is not read
MAX_DEPTH = 64                          # elements nested deeper than this are not a manifest

# field numbers: bundletool 1.18.3 config.proto (checked against `bundletool dump config` by cross_check) and aapt2's Resources.proto (checked against `dump manifest`)
SPARSE_ENCODING = {0: "UNSPECIFIED", 2: "VARIANT_FOR_SDK_32", 3: "DISABLED"}      # 1 is reserved in config.proto: bundletool rejects "ENFORCED", so never write it
BUNDLE_TYPES = {0: "REGULAR", 1: "APEX", 2: "ASSET_ONLY"}
CONFIG_KEYS = ("sparse_encoding", "collapse_resource_names", "deduplicate_resource_entries", "no_collapse_resources", "uncompressed_glob")
SUMMARY_KEYS = ("package", "versionCode", "versionName", "minSdk", "targetSdk", "debuggable")
ORACLES = ("jarsigner", "validate", "manifest", "config")


# ===================================================================================================== protobuf wire format
class ProtoError(ValueError):
    pass


def _varint(b, p):
    r = s = 0
    while True:
        if p >= len(b):
            raise ProtoError("truncated varint")
        c = b[p]
        p += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, p
        s += 7
        if s > 63:
            raise ProtoError("a varint longer than 10 bytes")


def fields(b):
    """Every field of one message as (number, wire type, value): an int for a varint, bytes for the rest (the payload of a length-delimited field, or the 4 / 8 raw
    bytes of a fixed32 / fixed64).  Groups are not used by any proto read here and are refused, like a field that runs past the end of its message."""
    p, n = 0, len(b)
    while p < n:
        key, p = _varint(b, p)
        num, wire = key >> 3, key & 7
        if num == 0:
            raise ProtoError("field number 0")
        if wire == 0:
            v, p = _varint(b, p)
        elif wire == 2:
            ln, p = _varint(b, p)
            if ln > n - p:
                raise ProtoError("field %d is %d bytes long but only %d are left" % (num, ln, n - p))
            v, p = bytes(b[p:p + ln]), p + ln
        elif wire in (1, 5):
            ln = 8 if wire == 1 else 4
            if ln > n - p:
                raise ProtoError("field %d is cut short" % num)
            v, p = bytes(b[p:p + ln]), p + ln
        else:
            raise ProtoError("field %d has wire type %d (groups are not supported)" % (num, wire))
        yield num, wire, v


def msg(b):
    """one message as {field number: [values in order]}; a field no decoder asks for is simply never looked at (unknown fields are skipped)"""
    out = {}
    for num, _w, v in fields(b):
        out.setdefault(num, []).append(v)
    return out


def _subs(m, num):
    return [msg(v) for v in m.get(num, []) if isinstance(v, bytes)]


def _sub(m, num):
    s = _subs(m, num)
    return s[-1] if s else {}                    # protobuf: the last occurrence of a singular field wins


def _s(m, num, default=""):
    v = m.get(num)
    return v[-1].decode("utf-8", "replace") if v and isinstance(v[-1], bytes) else default


def _i(m, num, default=0):
    v = m.get(num)
    return v[-1] if v and isinstance(v[-1], int) else default


# ===================================================================================================== BundleConfig.pb
def decode_config(blob):
    """BundleConfig.pb -> the settings apkforge cares about.  BundleConfig {bundletool=1 {version=2}, optimizations=2, compression=3, type=8};
    Optimizations.resource_optimizations=5 {sparse_encoding=1, collapsed_resource_names=2 {collapse_resource_names=1, no_collapse_resources=2 {type=1, name=2},
    deduplicate_resource_entries=3}}; Compression.uncompressed_glob=1."""
    top = msg(blob)
    ro = _sub(_sub(top, 2), 5)
    cn = _sub(ro, 2)
    sparse, kind = _i(ro, 1), _i(top, 8)
    return {
        "bundletool_version": _s(_sub(top, 1), 2),
        "type": BUNDLE_TYPES.get(kind, "UNKNOWN(%d)" % kind),
        "sparse_encoding": SPARSE_ENCODING.get(sparse, "UNKNOWN(%d)" % sparse),
        "collapse_resource_names": bool(_i(cn, 1)),
        "deduplicate_resource_entries": bool(_i(cn, 3)),
        "no_collapse_resources": sorted("%s/%s" % (_s(r, 1), _s(r, 2)) for r in _subs(cn, 2)),
        "uncompressed_glob": [v.decode("utf-8", "replace") for v in _sub(top, 3).get(1, []) if isinstance(v, bytes)],
    }


def config_from_json(j):
    """the same shape as decode_config(), from a BundleConfig.json (what `--config` takes) or from the JSON `bundletool dump config` prints"""
    ro = (j.get("optimizations") or {}).get("resourceOptimizations") or {}
    cn = ro.get("collapsedResourceNames") or {}
    return {
        "bundletool_version": (j.get("bundletool") or {}).get("version", ""),
        "type": j.get("type", "REGULAR"),
        "sparse_encoding": ro.get("sparseEncoding", "UNSPECIFIED"),
        "collapse_resource_names": bool(cn.get("collapseResourceNames", False)),
        "deduplicate_resource_entries": bool(cn.get("deduplicateResourceEntries", False)),
        "no_collapse_resources": sorted("%s/%s" % (r.get("type", ""), r.get("name", "")) for r in cn.get("noCollapseResources") or []),
        "uncompressed_glob": list((j.get("compression") or {}).get("uncompressedGlob") or []),
    }


def config_diffs(mine, theirs, keys=CONFIG_KEYS):
    return ["%s: %s against %s" % (k, json.dumps(mine.get(k)), json.dumps(theirs.get(k))) for k in keys if mine.get(k) != theirs.get(k)]


def bundle_config(opt, uncompressed_ext=None):
    """The BundleConfig.json for `bundletool build-bundle --config`: the resource optimization of "optimizeResources" (a resopt.Options, or None) plus the file extension
    React Native's bytecode needs stored.  -> (dict, or None when there is nothing to say; [notes for the log]).
    Mapping: sparseEncoding -> VARIANT_FOR_SDK_32 (a variant for SDK 32+, so no minSdk gate); collapseNames -> collapsedResourceNames, "keep" -> noCollapseResources,
    dedupe -> deduplicateResourceEntries (aapt2 only dedupes together with collapse, and so does this); shortenPaths has no bundletool option."""
    cfg, notes = {}, []
    if opt is not None:
        ro = {}
        if opt.sparse:
            ro["sparseEncoding"] = "VARIANT_FOR_SDK_32"
        if opt.collapse:
            c = {"collapseResourceNames": True}
            if opt.dedupe:
                c["deduplicateResourceEntries"] = True
            if opt.keep:
                c["noCollapseResources"] = [dict(zip(("type", "name"), k.split("/", 1))) for k in opt.keep]
            ro["collapsedResourceNames"] = c
        if ro:
            cfg["optimizations"] = {"resourceOptimizations": ro}
        if opt.shorten:
            notes.append('optimizeResources "shortenPaths": bundletool has no option for it (as of 1.18.3), so the APKs Google Play makes from this bundle keep their res/ paths; '
                         'the APK apkforge builds beside it is shortened')
    if uncompressed_ext:
        cfg["compression"] = {"uncompressedGlob": ["**." + uncompressed_ext]}
    return (cfg or None), notes


def describe_config(cfg):
    """one line for the log: what the bundle config asks bundletool for"""
    if not cfg:
        return "none"
    parts = []
    ro = (cfg.get("optimizations") or {}).get("resourceOptimizations") or {}
    if ro.get("sparseEncoding"):
        parts.append("sparse resource tables for SDK 32+")
    cn = ro.get("collapsedResourceNames")
    if cn:
        parts.append("resource names collapsed (%d kept)%s" % (len(cn.get("noCollapseResources", [])), ", duplicate values merged" if cn.get("deduplicateResourceEntries") else ""))
    glob = (cfg.get("compression") or {}).get("uncompressedGlob")
    if glob:
        parts.append("stored uncompressed: " + ", ".join(glob))
    return "; ".join(parts) or "none"


def describe_decoded(c):
    """one phrase for a claim: what a decoded BundleConfig.pb (decode_config) asks bundletool for"""
    parts = []
    if c.get("sparse_encoding") not in (None, "UNSPECIFIED", "DISABLED"):
        parts.append("sparse resource tables (%s)" % c["sparse_encoding"])
    if c.get("collapse_resource_names"):
        parts.append("resource names collapsed (%d kept)%s" % (len(c.get("no_collapse_resources", [])), ", duplicate values merged" if c.get("deduplicate_resource_entries") else ""))
    if c.get("uncompressed_glob"):
        parts.append("stored uncompressed: " + ", ".join(c["uncompressed_glob"]))
    return "; ".join(parts) or "bundletool defaults"


# ===================================================================================================== the proto manifest
def _element(m, depth):
    if depth > MAX_DEPTH:
        raise ProtoError("elements nested deeper than %d" % MAX_DEPTH)
    name = _s(m, 3)
    if not name:
        raise ProtoError("an element without a name")
    uri = _s(m, 2)
    el = ET.Element("{%s}%s" % (uri, name) if uri else name)
    for a in _subs(m, 4):
        an = _s(a, 2)
        if an:
            au = _s(a, 1)
            el.set("{%s}%s" % (au, an) if au else an, _s(a, 3))
    for c in _subs(m, 5):
        if 1 in c:                                  # a child XmlNode is an element (1) or text (2): text is only whitespace between elements
            el.append(_element(_sub(c, 1), depth + 1))
    return el


def decode_manifest(blob):
    """aapt2's proto manifest -> an ElementTree element shaped like manifest.parse()'s (attributes keyed {namespace}name), so manifest.launchers() reads it too.
    XmlNode {element=1, text=2, source=3}; XmlElement {namespace_declaration=1, namespace_uri=2, name=3, attribute=4, child=5}; XmlAttribute {namespace_uri=1, name=2, value=3}."""
    top = msg(blob)
    if 1 not in top:
        raise ProtoError("the manifest holds no element")
    return _element(_sub(top, 1), 0)


def manifest_summary(root):
    app, sdk = root.find("application"), root.find("uses-sdk")

    def num(el, attr):
        try:
            return int(el.get(M.A + attr)) if el is not None else None
        except (TypeError, ValueError):
            return None                              # a code name (a preview SDK) or an @reference: not a number
    return {"package": root.get("package"), "versionCode": num(root, "versionCode"), "versionName": root.get(M.A + "versionName"),
            "minSdk": num(sdk, "minSdkVersion"), "targetSdk": num(sdk, "targetSdkVersion"),
            "debuggable": app is not None and app.get(M.A + "debuggable") == "true"}


# ===================================================================================================== the bundle
def read_bundle(path):
    """-> (bundle, root).  bundle = {package, versionCode, versionName, minSdk, targetSdk, debuggable, launchers, modules, config, problems}; root = the base module's manifest as
    an ElementTree element (None when it could not be read).  Never raises for a bad file: the file comes back with a problem."""
    b = {"package": None, "versionCode": None, "versionName": None, "minSdk": None, "targetSdk": None, "debuggable": None,
         "launchers": [], "modules": [], "config": None, "problems": []}
    root = None
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            b["modules"] = sorted({n.split("/")[0] for n in names if n.count("/") == 2 and n.endswith("/manifest/AndroidManifest.xml")})

            def read(name):
                i = z.getinfo(name)
                if i.file_size > READ_CAP:
                    raise ProtoError("%s is %s: too large to be what it claims to be" % (name, util.human(i.file_size)))
                return z.read(i)
            if "base" not in b["modules"]:
                b["problems"].append("there is no base module (base/manifest/AndroidManifest.xml)")
            else:
                try:
                    root = decode_manifest(read("base/manifest/AndroidManifest.xml"))
                    b.update(manifest_summary(root))
                    b["launchers"] = M.launchers(root)
                except ProtoError as e:
                    b["problems"].append("base/manifest/AndroidManifest.xml cannot be read as a proto manifest: %s" % e)
            if "BundleConfig.pb" not in names:
                b["problems"].append("there is no BundleConfig.pb")
            else:
                try:
                    b["config"] = decode_config(read("BundleConfig.pb"))
                except ProtoError as e:
                    b["problems"].append("BundleConfig.pb cannot be read: %s" % e)
    except Exception as e:                           # fails closed, like verify_aab: whatever this cannot read becomes a problem
        b["problems"].append("cannot read this file as an App Bundle: %s: %s" % (type(e).__name__, e))
    return b, root


def play_findings(bundle, sig, unsigned_ok=False):
    """What Google Play will object to, from the decoded bundle and signing.verify_aab()'s result.  -> {"errors": [...], "warnings": [...]}.
    errors are what Play refuses; warnings are what it may.  unsigned_ok: the bundle is unsigned on purpose (`build --no-sign-aab`)."""
    errors, warnings = [], []
    if not sig["signed"] and not unsigned_ok:
        errors.append("the bundle is not signed: Google Play needs a bundle signed with your upload key")
    for s in sig["signers"]:
        until = str(s.get("not_after") or "")[:10]
        if s.get("debug"):
            errors.append("signed with the debug certificate (%s): app stores, Google Play included, do not accept apps signed with it; "
                          "sign with your upload key (a \"uploadSigning\" or \"signing\" section in apkforge.json, and --release)" % s.get("subject"))
        elif until and until <= PLAY_CERT_UNTIL:
            warnings.append("the signing certificate ends on %s: Google Play's signing documentation asks for a validity period that ends after 22 October 2033 "
                            "(it does not say whether that holds for an upload key too)" % until)
    if bundle.get("debuggable"):
        errors.append("the manifest says android:debuggable=\"true\": the Play Console refuses debuggable uploads (build with --release)")
    pkg = bundle.get("package") or ""
    if pkg == "com.example" or pkg.startswith("com.example."):
        warnings.append("applicationId %s: Google Play does not accept packages that start with com.example" % pkg)
    return {"errors": errors, "warnings": warnings}


# ===================================================================================================== the external readers
def no_oracles():
    return {k: {"state": "missing", "text": ""} for k in ORACLES}


def run_bundle_oracles(tc, path):
    """Run the external readers that are installed on one bundle; never downloads, never uses apt.  jarsigner -verify is the independent reader of the signature,
    `bundletool validate` of the structure, and `bundletool dump manifest` / `dump config` are what apkforge's own decoders are checked against.
    -> {"jarsigner" | "validate" | "manifest" | "config": {"state": "ok" | "rejected" | "missing", "text": the tool's output without JVM start-up notices}}"""
    out = {}
    js = tc.jarsigner()
    if not js:
        out["jarsigner"] = {"state": "missing", "text": ""}
    else:
        rc, text = run([js, "-verify", "-verbose:summary", "-certs", path], check=False)
        text = util.tool_output(text)
        out["jarsigner"] = {"state": "ok" if rc == 0 and "jar verified" in text.lower() else "rejected", "text": text}      # an unsigned jar also exits 0: the words decide
    try:
        bt, java = tc.bundletool(), tc.java()
    except ForgeError:
        bt = java = None
    for key, args in (("validate", ("validate",)), ("manifest", ("dump", "manifest")), ("config", ("dump", "config"))):
        if not (bt and java):
            out[key] = {"state": "missing", "text": ""}
            continue
        rc, text = run([java] + tc.jvm_flags(1024) + ["-jar", bt] + list(args) + ["--bundle=" + path], check=False)
        out[key] = {"state": "ok" if rc == 0 else "rejected", "text": util.tool_output(text)}
    return out


def cross_check(bundle, root, ora):
    """apkforge's decoders against bundletool's dumps.  -> {"manifest": {"state", "diffs"}, "config": {...}}; state is "agree" | "differ" | "unchecked"."""
    out = {"manifest": {"state": "unchecked", "diffs": []}, "config": {"state": "unchecked", "diffs": []}}
    r = ora["manifest"]
    if r["state"] == "ok" and root is not None:
        try:
            theirs = M.parse(r["text"], "`bundletool dump manifest`")
        except ForgeError as e:
            out["manifest"]["diffs"] = [str(e)]
        else:
            t = manifest_summary(theirs)
            diffs = ["%s: apkforge reads %s, bundletool %s" % (k, json.dumps(bundle.get(k)), json.dumps(t[k])) for k in SUMMARY_KEYS if bundle.get(k) != t[k]]
            if M.launchers(theirs) != bundle["launchers"]:
                diffs.append("launcher activities: apkforge reads %s, bundletool %s" % (json.dumps(bundle["launchers"]), json.dumps(M.launchers(theirs))))
            out["manifest"] = {"state": "differ" if diffs else "agree", "diffs": diffs}
    r = ora["config"]
    if r["state"] == "ok" and bundle.get("config") is not None:
        try:
            j = json.loads(r["text"])
            if not isinstance(j, dict):
                raise ValueError("not an object")
        except ValueError:
            out["config"]["diffs"] = ["`bundletool dump config` printed something that is not a JSON object"]
        else:
            diffs = config_diffs(bundle["config"], config_from_json(j), CONFIG_KEYS + ("bundletool_version", "type"))
            out["config"] = {"state": "differ" if diffs else "agree", "diffs": diffs}
    return out


# ===================================================================================================== bundletool build-apks --mode=universal
def repack_universal(raw):
    """bundletool's universal.apk (unsigned, entries written the way bundletool likes) -> a zipw.ZipBuilder with the same entries, laid out the way Builder.assemble()
    lays out an APK: AndroidManifest.xml first; resources.arsc stored and 4-aligned; native libraries stored and 16 KB-aligned; every other entry stays stored when
    bundletool stored it (4-aligned) and deflated when it did not.  Signature files and directory entries are dropped.  Raises ValueError for a duplicate name."""
    zb = zipw.ZipBuilder()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        infos = [i for i in z.infolist() if not i.filename.endswith("/") and not signing.JAR_SKIP.match(i.filename)]
        infos.sort(key=lambda i: i.filename != "AndroidManifest.xml")          # stable: the rest keeps bundletool's order
        for i in infos:
            n, data = i.filename, z.read(i)
            if n == "resources.arsc":
                zb.add(n, data, store=True, align=4)
            elif n.startswith("lib/") and n.endswith(".so"):
                zb.add(n, data, store=True, align=zipw.PAGE_16K)
            else:
                zb.add(n, data, store=i.compress_type == zipfile.ZIP_STORED)
    return zb


def universal_apk(tc, aab, signer, min_sdk, work, v1=None):
    """`bundletool build-apks --mode=universal` on `aab` (no key: bundletool writes an unsigned APK and says so), then repack_universal() and apkforge's own v2/v3 signing
    with `signer` (and v1 when the bundle's minSdk is below 24, or v1=True).  -> (signed APK bytes, {"entries": n, "bundletool_bytes": n}).  `work` is a scratch folder."""
    bt = tc.bundletool()
    if not bt:
        raise ForgeError("bundletool is not available (needs network once, or set APKFORGE_BUNDLETOOL)")
    os.makedirs(work, exist_ok=True)
    apks = os.path.join(work, "universal.apks")
    run([tc.java()] + tc.jvm_flags(1024) + ["-jar", bt, "build-apks", "--bundle=" + aab, "--output=" + apks, "--mode=universal", "--overwrite"], what="bundletool build-apks")
    try:
        with zipfile.ZipFile(apks) as z:
            if "universal.apk" not in z.namelist():
                raise ForgeError("bundletool build-apks --mode=universal wrote no universal.apk (it wrote: %s)" % ", ".join(z.namelist()))
            raw = z.read("universal.apk")
    except (OSError, zipfile.BadZipFile) as e:
        raise ForgeError("bundletool build-apks wrote no readable .apks file: %s" % e)
    try:
        zb = repack_universal(raw)
    except (ValueError, zipfile.BadZipFile) as e:
        raise ForgeError("cannot repack bundletool's universal.apk: %s" % e)
    n = len(zb.names)
    want_v1 = (min_sdk is None or min_sdk < 24) if v1 is None else bool(v1)
    return signing.sign_zip(zb, signer, v1=want_v1, v2=True, v3=True), {"entries": n, "bundletool_bytes": len(raw)}
