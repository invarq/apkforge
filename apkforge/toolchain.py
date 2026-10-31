"""Find the build tools on this machine, or provision them into ~/.apkforge/toolchain.

Order for every tool: explicit environment override -> Android SDK -> already provisioned -> download.
Tools are downloaded from GitHub (Maven Central is a last resort for ecj); apt-get is used only with --allow-apt.
Every download is checked against apkforge/pins.json when a pin exists.
"""
import glob
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import zipfile
from urllib.parse import urljoin, urlparse

from . import util
from .util import ForgeError, detail, download, exe, home_dir, host_arch, host_os, info, ok, run, warn

HERE = os.path.dirname(os.path.abspath(__file__))

GH = "https://github.com"
SOURCES = {
    "lzhiyong": GH + "/lzhiyong/android-sdk-tools/releases/download/35.0.2/android-sdk-tools-static-{arch}.zip",
    "apktool": GH + "/iBotPeaches/Apktool/releases/download/v3.0.3/apktool_3.0.3.jar",
    "jadx": GH + "/skylot/jadx/releases/download/v1.5.6/jadx-1.5.6.zip",
    "bundletool": GH + "/google/bundletool/releases/download/1.18.3/bundletool-all-1.18.3.jar",
    "kotlin": GH + "/JetBrains/kotlin/releases/download/v2.4.21/kotlin-compiler-2.4.21.zip",
    "ksp": GH + "/google/ksp/releases/download/{v}/artifacts.zip",           # a Maven-layout zip of the release (~180 MB): the KSP2 command line is in it
    "sable": "https://raw.githubusercontent.com/Sable/android-platforms/master/android-{api}/android.jar",
    "reginer": "https://raw.githubusercontent.com/Reginer/aosp-android-jar/main/android-{api}/android.jar",
    # last-resort ecj source for machines that are not Debian/Ubuntu
    "ecj_maven": ["https://repo1.maven.org/maven2/org/eclipse/jdt/ecj/3.37.0/ecj-3.37.0.jar"],
}


KSP_VERSION = "2.3.12"       # the KSP2 release provisioned from GitHub; pins.json holds the sha256 of its artifacts.zip
KSP_GROUP = "com.google.devtools.ksp"
KSP_ARTIFACTS = ("symbol-processing-aa-embeddable", "symbol-processing-api", "symbol-processing-common-deps")


def _vsort(v):
    """sort key of a plain release version (2.3.12, or the old KSP scheme 2.0.10-1.0.24): numbers, dots and dashes only."""
    return [int(p) for p in re.split(r"[.\-]", v) if p.isdigit()]


def _pins():
    """pins.json ships inside the package; read it through the loader so it also works from a single-file .pyz."""
    try:
        import pkgutil
        raw = pkgutil.get_data(__package__ or "apkforge", "pins.json")
        if raw:
            return json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, ImportError, TypeError):
        pass
    try:
        with open(os.path.join(HERE, "pins.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


PINS = _pins()
TRUST_NEW = [False]


def _tofu_file():
    return os.path.join(home_dir(), "toolchain", "downloads.lock.json")


def tofu_load():
    try:
        with open(_tofu_file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _tofu_save(d):
    p = _tofu_file()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)


def _zip_payload(path):
    """validate= for fetch(): the payload must be a zip/jar (an HTML error page, a captive-portal page or a truncated file is not)."""
    if zipfile.is_zipfile(path):
        return
    with open(path, "rb") as f:
        head = f.read(64).lstrip().lower()
    raise ForgeError("not a zip/jar file (%s)" % ("it is an HTML page" if head.startswith((b"<!doctype", b"<html", b"<?xml")) else "%d bytes of something else" % os.path.getsize(path)))


def fetch(url, dest, pin_key, label=None, sha256=None, validate=None, sha1=None):
    """download() checked against, in order: the pin in pins.json; a checksum the publisher shipped next to the file (`sha256`).
    With neither, trust on first use: the checksum is recorded in <home>/toolchain/downloads.lock.json and a later download of
    the same URL with other bytes is refused. --trust-new (TRUST_NEW) accepts a pinned/recorded file whose bytes changed,
    loudly, and prints both checksums so pins.json can be updated. A checksum published next to the file is always enforced.
    validate: fn(path) that raises ForgeError when the payload is not what the caller needs (e.g. _zip_payload). It runs BEFORE anything is recorded
    in the lock, on every path (also under --trust-new): a rejected payload is deleted and never recorded, so an error page cannot wedge the lock."""
    pin = PINS.get(pin_key)
    check = None if (pin and TRUST_NEW[0]) else (pin or sha256)
    label = label or pin_key
    got = download(url, dest, sha256=check, label=label, overridable=bool(pin), sha1=sha1)       # a sha1 is the publisher's (the SDK repository manifest): always enforced, and the sha256 is recorded below
    if validate:
        try:
            validate(got)
        except ForgeError as e:
            os.remove(got)
            raise ForgeError("%s from %s is not usable: %s (nothing was recorded)" % (label, util._redact(url), str(e).splitlines()[0]))
    sha = util.sha256_file(got)
    if pin:
        if sha != pin:                                         # only reachable with --trust-new
            warn("--trust-new: %s does not match its pin in pins.json, accepted anyway\n         pinned %s\n         got    %s" % (label, pin, sha))
        return got
    if sha256:                                                 # verified against the publisher's checksum
        detail("%s verified against the checksum published with it (%s)" % (label, sha[:16]))
        return got
    lock = tofu_load()
    known = lock.get(url)
    if known and known != sha:
        if not TRUST_NEW[0]:
            os.remove(got)
            raise ForgeError("%s is not the file downloaded before from this URL\n  url      %s\n  recorded %s\n  now      %s\n"
                             "  (re-run with --trust-new to accept the new file)" % (label, url, known, sha))
        warn("--trust-new: %s differs from the copy downloaded before, accepted anyway\n         recorded %s\n         got      %s" % (label, known, sha))
    if known != sha:
        lock[url] = sha
        _tofu_save(lock)
        detail("unpinned download %s sha256=%s  (recorded in downloads.lock.json; add it to pins.json to pin it)" % (label, sha))
    return got


# ---------------------------------------------------------------------------------------------- CMake, Ninja, the NDK (native.py)
PYPI = "https://pypi.org/pypi"
CMAKE_VERSION = "3.31.10"        # the newest 3.x: CMake 4 refuses cmake_minimum_required below 3.5, which many Android projects still say; pins.json holds the sha256 of its wheels
NINJA_VERSION = "1.13.2"
NDK_MANIFEST = "https://dl.google.com/android/repository/repository2-3.xml"
NDK_ENV = ("ANDROID_NDK_ROOT", "ANDROID_NDK_HOME", "ANDROID_NDK", "NDK_ROOT")
SDK_LICENSE_URL = "https://developer.android.com/studio/terms"


def _is_musl():
    return host_os() == "linux" and platform.libc_ver()[0] != "glibc" and bool(glob.glob("/lib/ld-musl-*"))


def _wheel_tags(filename):
    return filename[:-4].split("-")[-1].split(".") if filename.endswith(".whl") else []


def _glibc_of(tag):
    m = re.match(r"manylinux_(\d+)_(\d+)_", tag)
    if m:
        return int(m.group(1)), int(m.group(2))
    return {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}.get(tag.split("_")[0], (9, 99))


def _tag_fits(tag, os_, arch, musl):
    if os_ == "linux":
        a = {"x86_64": "x86_64", "aarch64": "aarch64", "x86": "i686"}.get(arch)
        return bool(a) and tag.startswith("musllinux" if musl else "manylinux") and tag.endswith("_" + a)
    if os_ == "macos":
        return tag.startswith("macosx_") and (tag.endswith("_universal2") or tag.endswith("_" + {"x86_64": "x86_64", "aarch64": "arm64"}.get(arch, "?")))
    if os_ == "windows":
        return tag == {"x86_64": "win_amd64", "aarch64": "win_arm64", "x86": "win32"}.get(arch)
    return False


def pick_wheel(files, os_=None, arch=None, musl=None):
    """The wheel of PyPI's `urls` list (the JSON API) that runs on this host, or None.  Deterministic: a universal2 macOS wheel before an arch-specific one, the oldest glibc
    first (cmake offers manylinux_2_12 and manylinux_2_17), then the file name.  Yanked files and everything that is not a wheel are never picked."""
    os_, arch = os_ or host_os(), arch or host_arch()
    musl = _is_musl() if musl is None else musl
    best = None
    for f in files:
        name = f.get("filename", "")
        if f.get("packagetype") != "bdist_wheel" or f.get("yanked") or not name.endswith(".whl"):
            continue
        tags = [t for t in _wheel_tags(name) if _tag_fits(t, os_, arch, musl)]
        if not tags:
            continue
        key = (min(_glibc_of(t) for t in tags) if os_ == "linux" else (0, 0), 0 if any(t.endswith("_universal2") for t in tags) else 1, name)
        if best is None or key < best[0]:
            best = (key, f)
    return best[1] if best else None


def wheel_binary(root, name):
    """Candidates for the executable `name` in an unpacked wheel, best first: cmake keeps it in cmake/data/bin/, ninja in ninja-X.data/scripts/ (the probe of Update 10 found both)."""
    want = exe(name)
    found = []
    for dp, dn, fn in os.walk(root):
        dn.sort()
        if want in fn and os.path.basename(dp) in ("bin", "scripts"):
            found.append(os.path.join(dp, want))
    found.sort(key=lambda p: (os.path.basename(os.path.dirname(p)) != "bin", len(p), p))
    return found


def extract_zip_unix(archive, dest):
    """Unpack a .zip the way `unzip` does on Unix: members keep their permission bits and a symbolic link stays a link (the NDK's bin/clang is one: zipfile.extractall would
    write the link's TARGET TEXT into a plain file).  The refusals of util.safe_extract apply: an absolute path, a '..' component, a link that points outside dest.  A member is
    never written through a link of the archive (links are created last).  When anything is refused or fails, what this call extracted is removed again at the top level of dest."""
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    before = set(os.listdir(dest))
    name_of = os.path.basename(archive)

    def inside(p):
        rp = os.path.realpath(p)
        return rp == root or rp.startswith(root + os.sep)

    def undo():
        for n in set(os.listdir(dest)) - before:
            q = os.path.join(dest, n)
            if os.path.islink(q) or os.path.isfile(q):
                os.remove(q)
            else:
                shutil.rmtree(q, ignore_errors=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            links = []
            infos = zf.infolist()
            for zi in infos:
                n = zi.filename
                parts = re.split(r"[\\/]", n)
                if n.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", n) or ".." in parts or not inside(os.path.join(dest, n)):
                    raise ForgeError("unsafe path in archive %s: %s" % (name_of, n))
                if stat.S_ISLNK(zi.external_attr >> 16):
                    target = zf.read(zi).decode("utf-8", "replace")
                    base = os.path.dirname(os.path.join(dest, n.rstrip("/")))
                    if os.path.isabs(target) or not inside(os.path.join(base, target)):
                        raise ForgeError("link in archive %s points outside the destination: %s -> %s" % (name_of, n, target))
            for zi in infos:
                mode = zi.external_attr >> 16
                path = os.path.join(dest, zi.filename.rstrip("/"))
                if stat.S_ISLNK(mode):
                    links.append((path, zf.read(zi).decode("utf-8", "replace")))
                elif zi.is_dir():
                    os.makedirs(path, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with zf.open(zi) as src, open(path, "wb") as out:
                        shutil.copyfileobj(src, out)
                    os.chmod(path, (mode & 0o777) | 0o600 if mode & 0o777 else 0o644)
            for path, target in links:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if os.path.lexists(path):
                    os.remove(path)
                os.symlink(target, path)
    except ForgeError:
        undo()
        raise
    except (OSError, zipfile.BadZipFile, NotImplementedError) as e:
        undo()
        raise ForgeError("cannot unpack %s: %s: %s" % (name_of, type(e).__name__, " ".join(str(e).split())[:200]))


def ndk_revision(root):
    """Pkg.Revision of an NDK folder's source.properties (\"27.1.12297006\", \"28.0.12433566-rc1\"), or None."""
    try:
        with open(os.path.join(root, "source.properties"), encoding="utf-8", errors="replace") as f:
            m = re.search(r"^Pkg\.Revision\s*=\s*(\S+)\s*$", f.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def rev_key(rev):
    """sort key of an NDK revision: its numbers, and a release before a preview of the same numbers"""
    return [int(x) for x in re.findall(r"\d+", rev.split("-")[0])], "-" not in rev


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def ndk_packages(xml_text):
    """The NDK packages of an Android SDK repository manifest (repository2-*.xml):
    [{\"path\": \"ndk;27.1.12297006\", \"revision\": \"27.1.12297006\", \"stable\": bool, \"archives\": [{\"os\": \"linux\", \"arch\": None | \"x64\" | ..., \"url\", \"sha1\", \"size\"}]}].
    Written from memory of the schema and tested only against manifests made to that shape: NOT yet run against dl.google.com."""
    import xml.etree.ElementTree as ET
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        raise ForgeError("the SDK repository manifest declares a DOCTYPE or entities: refusing to parse it")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ForgeError("the SDK repository manifest is not valid XML: %s" % e)
    stable = {c.get("id") for c in root.iter() if _local(c.tag) == "channel" and (c.text or "").strip() == "stable"}
    out = []
    for rp in root.iter():
        if _local(rp.tag) != "remotePackage" or not (rp.get("path") or "").startswith("ndk;"):
            continue
        kids = {_local(c.tag): c for c in rp}
        rev = kids.get("revision")
        parts = [(next((x.text for x in rev if _local(x.tag) == k), None) or "").strip() for k in ("major", "minor", "micro")] if rev is not None else []
        preview = rev is not None and any(_local(x.tag) == "preview" for x in rev)
        revision = ".".join(parts) if parts and all(parts) else rp.get("path")[4:]
        chan = kids.get("channelRef")
        archives = []
        for ar in rp.iter():
            if _local(ar.tag) != "archive":
                continue
            c = {_local(x.tag): x for x in ar}
            comp = c.get("complete")
            if comp is None:
                continue
            cc = {_local(x.tag): x for x in comp}
            sha = (cc["checksum"].text or "").strip().lower() if "checksum" in cc and (cc["checksum"].get("type") in (None, "sha1")) else ""
            url = (cc["url"].text or "").strip() if "url" in cc else ""
            if not url or not re.fullmatch(r"[0-9a-f]{40}", sha):
                continue
            archives.append({"os": ((c["host-os"].text or "").strip() if "host-os" in c else ""), "arch": ((c["host-arch"].text or "").strip() if "host-arch" in c else None) or None,
                             "url": url, "sha1": sha, "size": int((cc["size"].text or "0").strip() or 0) if "size" in cc else 0})
        out.append({"path": rp.get("path"), "revision": revision, "stable": (chan is not None and chan.get("ref") in stable) and not preview, "archives": archives})
    return out


def pick_ndk_archive(pkg, os_=None, arch=None):
    """The archive of an NDK package for this host, or None.  A host-arch is honoured when the manifest names one; an archive without one is an x86_64 build (Google ships no
    Linux arm64 NDK: that is why the answer is None there), which macOS (Rosetta) and Windows (emulation) run on arm64 as well."""
    os_, arch = os_ or host_os(), arch or host_arch()
    want_os = {"linux": "linux", "macos": "macosx", "windows": "windows"}.get(os_)
    arch_names = {"x86_64": ("x64", "x86_64", "amd64"), "aarch64": ("aarch64", "arm64"), "x86": ("x86", "i686")}.get(arch, (arch,))
    named, plain = None, None
    for a in pkg["archives"]:
        if a["os"] != want_os:
            continue
        if a["arch"]:
            if a["arch"].lower() in arch_names and named is None:
                named = a
        elif plain is None:
            plain = a
    if named:
        return named
    if plain and (arch == "x86_64" or os_ in ("macos", "windows")):
        return plain
    return None


# ---------------------------------------------------------------------------------------------- the object
# the independent readers behind the `[confirmed]` tags, and the apt package for the ones whose package is not named like the tool
ORACLE_TOOLS = ("dexdump", "apksigner", "zipalign", "readelf")
ORACLE_APT = {"readelf": "binutils"}


class Toolchain:
    def __init__(self, fetch_missing=True, allow_apt=False):
        # allow_apt: apt-get installs packages system-wide (as root), so it is opt-in: --allow-apt or APKFORGE_ALLOW_APT=1.
        # Without it every tool is fetched into ~/.apkforge/toolchain instead.
        self.fetch_missing = fetch_missing
        self.apt_requested = bool(allow_apt)
        self.allow_apt = bool(allow_apt) and fetch_missing      # apt-get downloads packages: never under --offline
        self.root = os.path.join(home_dir(), "toolchain")
        os.makedirs(self.root, exist_ok=True)
        self._memo = {}
        self.notes = []
        self.vendor_dir = None          # <project>/vendor: set by the Builder, so a KSP runner vendored with the project is found first

    # ------------------------------------------------------------------ helpers
    def path(self, *p):
        return os.path.join(self.root, *p)

    def _memoize(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    # ------------------------------------------------------------------ java runtime
    def _jdk_tool(self, name):
        """`name` (java, javac, keytool, jarsigner: always the bare name) in the JDK that apkforge downloads into <toolchain>/jdk (provision_jdk).  Temurin unpacks
        to jdk-<version>/bin on Linux and Windows and to jdk-<version>/Contents/Home/bin on macOS."""
        n = exe(name)
        return glob.glob(self.path("jdk", "*", "bin", n)) + glob.glob(self.path("jdk", "*", "Contents", "Home", "bin", n))

    def java(self):
        """The `java` launcher: D8/R8, kotlinc and bundletool are Java programs.  Order: JAVA_HOME, PATH, the JDK apkforge downloaded.  A bare machine (a plain Docker
        image) has none of them, so unless downloads are off the JDK is fetched now, the way a compile step fetches javac.  Java is needed before any javac step has
        run (a fresh D8 is checked with it, the API guard asks it for the backported methods), so without the fetch a first `doctor` or `build` there stops with
        "no Java runtime found", and, once a JDK has been downloaded, without the lookup every later run still does."""
        def f():
            cands = []
            jh = os.environ.get("JAVA_HOME")
            if jh:                                      # unset: os.path.join("", "bin", "java") is bin/java of the current directory
                cands.append(os.path.join(jh, "bin", exe("java")))
            cands.append(util.which("java"))
            cands += self._jdk_tool("java")
            for cand in cands:
                if cand and os.path.isfile(cand):
                    return cand
            if self.fetch_missing:
                try:
                    javac = self.ensure_javac()
                    cand = os.path.join(os.path.dirname(javac), exe("java"))
                    if os.path.isfile(cand):
                        return cand
                    why = "; there is no java beside " + javac
                except ForgeError as e:
                    why = "; fetching a JDK failed: " + str(e).splitlines()[0]
            else:
                why = " (nothing is downloaded with --offline: run `apkforge doctor` once with network access)"
            raise ForgeError("no Java runtime found. Install a JRE/JDK 11 or newer (Debian/Ubuntu: apt-get install -y default-jre-headless) "
                             "or set JAVA_HOME%s." % why)
        return self._memoize("java", f)

    def java_major(self):
        def f():
            _, out = run([self.java(), "-version"], check=False)
            m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
            if not m:
                return 0
            return int(m.group(2)) if m.group(1) == "1" and m.group(2) else int(m.group(1))
        return self._memoize("jmajor", f)

    def jvm_flags(self, heap_mb=1536):
        """Short-lived compiler JVMs on a 1-CPU machine: serial GC and the C1 JIT only start much faster."""
        return ["-Xmx%dm" % heap_mb, "-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1", "-Xshare:auto"]

    # ------------------------------------------------------------------ Android SDK (optional)
    def sdk(self):
        def f():
            for v in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
                d = os.environ.get(v)
                if d and os.path.isdir(d):
                    return d
            for d in (os.path.expanduser("~/Android/Sdk"), os.path.expanduser("~/Library/Android/sdk"), "/opt/android-sdk", "/usr/lib/android-sdk"):
                if os.path.isdir(os.path.join(d, "build-tools")) or os.path.isdir(os.path.join(d, "platforms")):
                    return d
            return None
        return self._memoize("sdk", f)

    def _sdk_build_tools(self, need):
        sdk = self.sdk()
        if not sdk:
            return None
        dirs = sorted(glob.glob(os.path.join(sdk, "build-tools", "*")), key=lambda d: [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", os.path.basename(d))], reverse=True)
        for d in dirs:
            if os.path.exists(os.path.join(d, need)):
                return d
        return None

    # ------------------------------------------------------------------ aapt2
    def aapt2(self):
        def f():
            e = os.environ.get("APKFORGE_AAPT2")
            if e and os.path.isfile(e):
                return e
            d = self._sdk_build_tools(exe("aapt2"))
            if d:
                return os.path.join(d, exe("aapt2"))
            cached = self.path("aapt2", exe("aapt2"))
            if os.path.isfile(cached):
                return cached
            if not self.fetch_missing:
                raise ForgeError("aapt2 is not installed and --offline forbids downloading it. Run `apkforge doctor` once with network access, "
                                 "or point apkforge at one: APKFORGE_AAPT2=/path/to/aapt2 or ANDROID_HOME=<sdk>")
            return self._provision_aapt2(cached)
        return self._memoize("aapt2", f)

    def _provision_aapt2(self, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        errors = []
        if host_os() == "linux":
            arch = host_arch()
            url = SOURCES["lzhiyong"].format(arch=arch)
            try:
                z = fetch(url, self.path("dl", "lzhiyong-%s.zip" % arch), "lzhiyong-" + arch, "Android SDK tools (static, %s)" % arch, validate=_zip_payload)
                with zipfile.ZipFile(z) as zf:
                    for member in ("build-tools/aapt2", "build-tools/zipalign"):   # its static dexdump aborts here (libartpalette) - never used
                        if member in zf.namelist():
                            target = os.path.join(os.path.dirname(dest), os.path.basename(member))
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            os.chmod(target, 0o755)
                ok("aapt2 provisioned from lzhiyong/android-sdk-tools 35.0.2 (static build)")
                return dest
            except (ForgeError, zipfile.BadZipFile, KeyError) as e:
                errors.append(str(e).splitlines()[0])
        key = {"linux": "linux", "macos": "macosx", "windows": "windows"}.get(host_os())
        if key:
            try:
                jar = fetch(SOURCES["apktool"], self.path("dl", "apktool.jar"), "apktool", "Apktool 3.0.3 (carries aapt2)", validate=_zip_payload)
                member = "prebuilt/%s/%s" % (key, "aapt2.exe" if key == "windows" else "aapt2")
                with zipfile.ZipFile(jar) as zf, zf.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(dest, 0o755)
                ok("aapt2 provisioned from Apktool 3.0.3 (%s)" % key)
                return dest
            except (ForgeError, zipfile.BadZipFile, KeyError) as e:
                errors.append(str(e).splitlines()[0])
        raise ForgeError("cannot provision aapt2 (%s). Install the Android SDK build-tools and set ANDROID_HOME, "
                         "or set APKFORGE_AAPT2=/path/to/aapt2" % "; ".join(errors))

    def aapt2_version(self):
        a = self.aapt2()
        return self._memoize("aapt2v", lambda: run([a, "version"], check=False)[1].strip().splitlines()[0] if a else "")

    # ------------------------------------------------------------------ D8 / R8
    def dexer(self):
        """Path of a jar that holds com.android.tools.r8.D8 and R8."""
        def f():
            e = os.environ.get("APKFORGE_R8_JAR")
            if e and os.path.isfile(e):
                return e
            d = self._sdk_build_tools("lib/d8.jar")
            if d:
                return os.path.join(d, "lib", "d8.jar")
            cached = self.path("dexer", "r8-d8.jar")
            if os.path.isfile(cached):
                return cached
            if not self.fetch_missing:
                raise ForgeError("D8/R8 is not installed and --offline forbids downloading it. Run `apkforge doctor` once with network access, "
                                 "or point apkforge at one: APKFORGE_R8_JAR=/path/to/d8.jar or ANDROID_HOME=<sdk>")
            return self._provision_dexer(cached)
        return self._memoize("dexer", f)

    @staticmethod
    def carve_r8(src_jar, dest_jar):
        """Copy only the R8/D8 classes (they are self-contained: dependencies are shaded inside) out of a fat jar."""
        n = 0
        tmp = dest_jar + ".part"
        with zipfile.ZipFile(src_jar) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
            for info_ in zin.infolist():
                name = info_.filename
                if name.startswith("com/android/tools/r8/") or name == "r8-version.properties" or name.startswith("META-INF/services/com.android.tools.r8"):
                    zi = zipfile.ZipInfo(name, (2008, 1, 1, 0, 0, 0))
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zout.writestr(zi, zin.read(name))
                    n += 1
        os.replace(tmp, dest_jar)
        return n

    def _provision_dexer(self, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        errors = []
        # dexer() trusts whatever file sits at `dest`.  The jar is therefore carved beside it and moved there only once it has started: a run that cannot start Java
        # must not leave an unchecked jar behind (the second source below would also overwrite the first with its older D8 3.3) for every later run to use.
        cand = os.path.join(os.path.dirname(dest), "candidate-" + os.path.basename(dest))
        # 1) newest D8 (9.x) lives inside jadx's fat jar; 2) bundletool carries an older one (3.3) - both self-contained
        for key, label, inner in (("jadx", "jadx 1.5.6 (carries D8 9.1)", "lib/jadx-1.5.6-all.jar"),
                                  ("bundletool", "bundletool 1.18.3 (carries D8 3.3)", None)):
            try:
                z = fetch(SOURCES[key], self.path("dl", os.path.basename(SOURCES[key])), key, label, validate=_zip_payload)
                src = z
                if inner:
                    with zipfile.ZipFile(z) as zf:
                        src = self.path("dl", "jadx-all.jar")
                        with zf.open(inner) as a, open(src, "wb") as b:
                            shutil.copyfileobj(a, b)
                n = self.carve_r8(src, cand)
                if inner and os.path.exists(src):
                    os.remove(src)
                _, out = run([self.java(), "-cp", cand, "com.android.tools.r8.D8", "--version"], check=False)
                if "D8" not in util.tool_output(out):
                    raise ForgeError("carved jar does not start: " + out.strip()[:200])
                os.replace(cand, dest)
                ok("D8 provisioned: %s (%d classes/files, %s)" % (util.tool_output(out).strip().splitlines()[0][:60], n, util.human(os.path.getsize(dest))))
                return dest
            except (ForgeError, zipfile.BadZipFile, KeyError) as e:
                errors.append("%s: %s" % (key, str(e).splitlines()[0]))
            finally:
                if os.path.exists(cand):
                    os.remove(cand)
        raise ForgeError("cannot provision D8 (%s). Set APKFORGE_R8_JAR=/path/to/r8.jar or use an Android SDK (ANDROID_HOME)." % "; ".join(errors))

    def dexer_version(self):
        d = self.dexer()
        def f():
            _, out = run([self.java(), "-cp", d, "com.android.tools.r8.D8", "--version"], check=False)
            out = util.tool_output(out)
            return out.strip().splitlines()[0][:48] if out.strip() else "?"
        return self._memoize("dexerv", f) if d else ""

    # ------------------------------------------------------------------ android.jar
    def android_jar(self, api):
        def f():
            e = os.environ.get("APKFORGE_ANDROID_JAR")
            if e and os.path.isfile(e):
                return e
            sdk = self.sdk()
            if sdk:
                p = os.path.join(sdk, "platforms", "android-%s" % api, "android.jar")
                if os.path.isfile(p):
                    return p
            cached = self.path("android-jars", "android-%s.jar" % api)
            if os.path.isfile(cached):
                return cached
            if not self.fetch_missing:
                raise ForgeError("android.jar for API %s is not installed and --offline forbids downloading it. Run `apkforge doctor` once with network access, "
                                 "or point apkforge at one: APKFORGE_ANDROID_JAR=/path/to/android.jar or ANDROID_HOME=<sdk>" % api)
            return self._provision_android_jar(api, cached)
        return self._memoize("ajar%s" % api, f)

    def android_jar_exact(self, api, compile_api=None):
        """android.jar for exactly API level `api`, for COMPARING levels (the min-API guard). Refuses what would make the comparison meaningless:
        APKFORGE_ANDROID_JAR names one jar for every level (android_jar() returns it for all of them, so the guard would compare the compile jar
        with itself and say nothing), and a jar that came from the hidden-API fallback (every hidden API looks present, so a newer API would pass
        as an old one). Raises ForgeError."""
        e = os.environ.get("APKFORGE_ANDROID_JAR")
        if e and os.path.isfile(e) and str(api) != str(compile_api):
            raise ForgeError("APKFORGE_ANDROID_JAR names one jar for every API level, so it cannot stand in for android.jar %s (the compile jar is API %s). "
                             "Unset it, or use ANDROID_HOME with the platforms installed, to enable the min-API guard" % (api, compile_api))
        p = self.android_jar(api)
        if os.path.isfile(p + ".hidden-api"):
            raise ForgeError("android.jar %s here is the AOSP build with hidden APIs (the official stubs could not be fetched), and hidden APIs make newer APIs "
                             "look present. Delete %s and %s.hidden-api, then run online" % (api, p, p))
        return p

    def backported_methods(self, api):
        """The methods D8 rewrites away at minSdk `api` (R8's BackportedMethodList) as 'owner#name(desc)' strings, cached per (dexer, api).
        When the list cannot be had: warn once and return an empty set, so the guard reports those calls (the safe direction)."""
        def f():
            try:
                cache = self.path("backports", "%s-%s.txt" % (self.ident("dexer"), api))
                if not os.path.isfile(cache):
                    os.makedirs(os.path.dirname(cache), exist_ok=True)
                    tmp = cache + ".tmp"
                    rc, out = run([self.java(), "-cp", self.dexer(), "com.android.tools.r8.BackportedMethodList", "--min-api", str(api), "--output", tmp], check=False)
                    if rc != 0 or not os.path.isfile(tmp) or not os.path.getsize(tmp):
                        raise ForgeError("BackportedMethodList failed: %s" % (out.strip().splitlines() or ["no output"])[-1][:120])
                    os.replace(tmp, cache)
                with open(cache, encoding="utf-8") as fh:
                    return frozenset(l.strip() for l in fh if l.strip())
            except ForgeError as e:
                warn("min-API guard: the list of methods D8 backports at minSdk %s is unavailable (%s); calls D8 would rewrite are reported as unguarded"
                     % (api, str(e).splitlines()[0][:120]))
                return frozenset()
        return self._memoize("backports%s" % api, f)

    def _provision_android_jar(self, api, dest):
        errors = []
        marker = dest + ".hidden-api"      # present exactly when dest is the hidden-API fallback: android_jar_exact() refuses such a jar
        for key, why in (("sable", "official SDK stubs (Sable/android-platforms)"),
                         ("reginer", "AOSP build WITH hidden APIs (Reginer/aosp-android-jar): code may compile that crashes at runtime")):
            try:
                if key != "sable":
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(marker, "w") as f:                       # written BEFORE the download: a hidden-API jar can never exist without it
                        f.write("this android.jar is the AOSP build with hidden APIs, not the official SDK stubs\n")
                fetch(SOURCES[key].format(api=api), dest, "%s-android-%s" % (key, api), "android.jar API %s" % api, validate=_zip_payload)
                if key == "sable" and os.path.exists(marker):
                    os.remove(marker)
                (ok if key == "sable" else warn)("android.jar API %s: %s" % (api, why))
                return dest
            except ForgeError as e:
                if key != "sable" and os.path.exists(marker):
                    os.remove(marker)
                errors.append("%s: %s" % (key, str(e).splitlines()[0]))
        if str(api) == "23" and self.allow_apt and util.which("apt-get"):
            try:
                d = self._apt_extract("libandroid-23-java")
                src = os.path.join(d, "usr/share/java/com.android.android-23.jar")
                shutil.copyfile(src, dest)
                warn("android.jar API 23 from Ubuntu's libandroid-23-java")
                return dest
            except ForgeError as e:
                errors.append("apt: " + str(e).splitlines()[0])
        raise ForgeError("cannot get android.jar for API %s (%s). Put your own at %s, or set APKFORGE_ANDROID_JAR / ANDROID_HOME."
                         % (api, "; ".join(errors), dest))

    # ------------------------------------------------------------------ apt helpers (Debian/Ubuntu only)
    def _apt_extract(self, pkg):
        """Download a .deb with apt-get download and unpack it (no install, no root needed once the lists exist)."""
        dest = self.path("apt", pkg)
        if os.path.isdir(dest) and os.listdir(dest):
            return dest
        if not (util.which("apt-get") and util.which("dpkg-deb")):
            raise ForgeError("apt-get/dpkg-deb not available")
        dl = self.path("dl", "deb-" + pkg)
        os.makedirs(dl, exist_ok=True)
        for f in glob.glob(os.path.join(dl, "*.deb")):
            os.remove(f)
        rc, out = run(["apt-get", "download", pkg], cwd=dl, check=False)
        if rc != 0 and os.geteuid() == 0 if hasattr(os, "geteuid") else False:
            info("apt-get update  (changes the system's apt lists; allowed by --allow-apt)")
            run(["apt-get", "update", "-qq"], check=False)
            rc, out = run(["apt-get", "download", pkg], cwd=dl, check=False)
        debs = glob.glob(os.path.join(dl, "*.deb"))
        if rc != 0 or not debs:
            raise ForgeError("apt-get download %s failed: %s" % (pkg, out.strip()[:160]))
        os.makedirs(dest, exist_ok=True)
        run(["dpkg-deb", "-x", debs[0], dest], what="dpkg-deb")
        return dest

    # ------------------------------------------------------------------ javac / ecj
    def javac(self):
        def f():
            cands = []
            jh = os.environ.get("JAVA_HOME")
            if jh:
                cands.append(os.path.join(jh, "bin", exe("javac")))
            cands.append(util.which("javac"))
            cands += sorted(glob.glob("/usr/lib/jvm/*/bin/javac"), reverse=True)
            cands += self._jdk_tool("javac")
            for c in cands:
                if c and os.path.isfile(c):
                    return c
            return None
        return self._memoize("javac", f)

    def _installed_jdk_tool(self, name):
        """`name` (jarsigner, keytool: the bare name) from a JDK that is already installed; never downloads.  Order: JAVA_HOME, PATH, beside the javac apkforge
        compiles with, /usr/lib/jvm, the JDK apkforge downloaded.  None when there is none."""
        n = exe(name)
        cands = []
        jh = os.environ.get("JAVA_HOME")
        if jh:
            cands.append(os.path.join(jh, "bin", n))
        cands.append(util.which(name))
        j = self.javac()
        if j:
            cands.append(os.path.join(os.path.dirname(j), n))
        cands += sorted(glob.glob("/usr/lib/jvm/*/bin/" + n), reverse=True)
        cands += self._jdk_tool(name)
        for c in cands:
            if c and os.path.isfile(c):
                return c
        return None

    def jarsigner(self):
        """jarsigner of a JDK: the independent reader of the JAR signature of an App Bundle (`apkforge verify x.aab`, and the check after `build --aab`).  None when only a
        JRE is installed.  Never downloads: a missing jarsigner only leaves the signature claim [internal only]."""
        return self._memoize("jarsigner", lambda: self._installed_jdk_tool("jarsigner"))

    def keytool(self):
        """keytool, which turns a JKS keystore into PKCS#12 for signing (signing.keystore_signer).  Found like jarsigner(), so the JDK that `apkforge doctor` downloaded
        counts too.  None when there is none; never downloads."""
        return self._memoize("keytool", lambda: self._installed_jdk_tool("keytool"))

    def ecj(self):
        """Eclipse compiler for Java: a javac replacement that only needs a JRE."""
        def f():
            e = os.environ.get("APKFORGE_ECJ_JAR")
            if e and os.path.isfile(e):
                return e
            cached = self.path("ecj", "ecj.jar")
            if os.path.isfile(cached):
                return cached
            if not self.fetch_missing:
                return None
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            errors = []
            if self.allow_apt and util.which("apt-get"):
                try:
                    # NOT libecj-java: stale 3.16 on Ubuntu 24.04. The 3.32 compiler is the jar of libeclipse-jdt-core-java.
                    d = self._apt_extract("libeclipse-jdt-core-java")
                    real = [j for j in glob.glob(os.path.join(d, "usr/share/java/eclipse-jdt-core-*.jar")) if os.path.isfile(j) and not os.path.islink(j)]
                    if real:
                        shutil.copyfile(real[0], cached)
                        ok("ecj provisioned from the Ubuntu package libeclipse-jdt-core-java (%s)" % os.path.basename(real[0]))
                        return cached
                    errors.append("apt: jar not found in the package")
                except ForgeError as e:
                    errors.append("apt: " + str(e).splitlines()[0])
            for url in SOURCES["ecj_maven"]:
                try:
                    fetch(url, cached, "ecj", "ecj", validate=_zip_payload)
                    ok("ecj provisioned from " + url)
                    return cached
                except ForgeError as e:
                    errors.append(str(e).splitlines()[0])
            return None
        return self._memoize("ecj", f)

    def provision_jdk(self):
        """Install a full JDK (javac) into the toolchain folder from Temurin on GitHub (no root needed)."""
        have = self.javac()
        if have:
            return have
        os_, arch = host_os(), host_arch()
        plat = {("linux", "x86_64"): "x64_linux", ("linux", "aarch64"): "aarch64_linux", ("macos", "x86_64"): "x64_mac",
                ("macos", "aarch64"): "aarch64_mac", ("windows", "x86_64"): "x64_windows"}.get((os_, arch))
        if not plat:
            raise ForgeError("no Temurin JDK build known for %s/%s" % (os_, arch))
        repo = "adoptium/temurin21-binaries"
        if not self.fetch_missing:
            raise ForgeError("--offline forbids downloading a JDK")
        loc = util.redirect_location("%s/%s/releases/latest" % (GH, repo))
        if loc is None:
            raise ForgeError("unexpected response while looking for the latest Temurin 21 tag")
        tag = loc.rsplit("/", 1)[-1]
        if not tag:
            raise ForgeError("cannot find the latest Temurin 21 release tag")
        page = util.fetch_text("%s/%s/releases/expanded_assets/%s" % (GH, repo, tag))
        ext = "zip" if os_ == "windows" else "tar.gz"
        names = sorted(set(re.findall(r'releases/download/[^"]+/(OpenJDK21U-jdk_%s_hotspot_[^"/]+\.%s)"' % (plat, re.escape(ext)), page)))
        if not names:
            raise ForgeError("no JDK asset for %s in release %s" % (plat, tag))
        url = "%s/%s/releases/download/%s/%s" % (GH, repo, tag, names[0])
        published = None
        try:                                                   # every Temurin asset has a <name>.sha256.txt next to it
            m = re.match(r"\s*([0-9a-fA-F]{64})\b", util.fetch_text(url + ".sha256.txt"))
            published = m.group(1).lower() if m else None
        except ForgeError:
            pass
        if not published:
            warn("no published checksum found for %s: the JDK download is not verified" % names[0])
        arc = fetch(url, self.path("dl", names[0]), "temurin-" + plat, "Temurin JDK 21 (%s)" % tag, sha256=published)
        dest = self.path("jdk")
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        util.safe_extract(arc, dest)
        self._memo.pop("javac", None)
        j = self.javac()
        if not j:
            raise ForgeError("JDK unpacked but javac not found in %s" % dest)
        ok("JDK provisioned: " + j)
        return j

    def ensure_javac(self):
        """javac is the compiler (ecj cannot compile against android.jar at Java 9+). Order: found -> apt (root) -> Temurin."""
        have = self.javac()
        if have:
            return have
        if not self.fetch_missing:
            raise ForgeError("no javac found and --offline forbids downloading a JDK%s. Run `apkforge doctor` once with network access, "
                             "or set JAVA_HOME to a JDK 17 or newer" % (" (apt is not used either: --allow-apt is ignored under --offline)" if self.apt_requested else ""))
        errs = []
        if self.allow_apt and hasattr(os, "geteuid") and os.geteuid() == 0 and util.which("apt-get"):
            try:
                return self.provision_jdk_apt()
            except ForgeError as e:
                errs.append(str(e).splitlines()[0])
        try:
            return self.provision_jdk()
        except ForgeError as e:
            errs.append(str(e).splitlines()[0])
        if not self.allow_apt and util.which("apt-get") and hasattr(os, "geteuid") and os.geteuid() == 0:
            errs.append("on Debian/Ubuntu, --allow-apt (or APKFORGE_ALLOW_APT=1) lets apkforge apt-get install openjdk-21-jdk-headless")
        raise ForgeError("no javac and cannot install one (%s)" % "; ".join(errs))

    def jdk_home(self):
        j = self.javac()
        return os.path.dirname(os.path.dirname(os.path.realpath(j))) if j else None

    def oracles(self):
        """Independent verifiers from Ubuntu: dexdump, apksigner, zipalign, readelf. Optional, root + apt only."""
        out = {}
        need = [t for t in ORACLE_TOOLS if not (self.zipalign() if t == "zipalign" else util.which(t))]
        if need and self.allow_apt and hasattr(os, "geteuid") and os.geteuid() == 0 and util.which("apt-get"):
            pkgs = [ORACLE_APT.get(t, t) for t in need]
            info("apt-get install %s  (system-wide; allowed by --allow-apt)" % " ".join(pkgs))
            run(["apt-get", "install", "-y", "--no-install-recommends"] + pkgs, check=False, timeout=600, what="apt-get install oracles")
        for t in ORACLE_TOOLS:
            out[t] = (self.zipalign() if t == "zipalign" else None) or util.which(t)      # zipalign: the copy the builds really use (F2: doctor said "missing" while it was installed)
        return out

    def provision_jdk_apt(self):
        if not (self.allow_apt and util.which("apt-get") and hasattr(os, "geteuid") and os.geteuid() == 0):
            raise ForgeError("apt install needs Debian/Ubuntu and root; use the Temurin route instead")
        info("apt-get update, then apt-get install openjdk-21-jdk-headless  (system-wide; allowed by --allow-apt)")
        run(["apt-get", "update", "-qq"], check=False)
        run(["apt-get", "install", "-y", "--no-install-recommends", "openjdk-21-jdk-headless"], what="apt-get install openjdk-21-jdk-headless", timeout=900)
        self._memo.pop("javac", None)
        j = self.javac()
        if not j:
            raise ForgeError("openjdk installed but javac not found")
        ok("JDK installed with apt: " + j)
        return j

    # ------------------------------------------------------------------ Kotlin
    def kotlinc_home(self):
        def f():
            e = os.environ.get("KOTLIN_HOME")
            if e and os.path.isdir(os.path.join(e, "lib")):
                return e
            cached = self.path("kotlinc")
            if os.path.isfile(os.path.join(cached, "bin", "kotlinc")):
                return cached
            if not self.fetch_missing:
                return None
            z = fetch(SOURCES["kotlin"], self.path("dl", "kotlin-compiler.zip"), "kotlin", "Kotlin compiler 2.4.21", validate=_zip_payload)
            tmp = self.path("kotlinc.tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            with zipfile.ZipFile(z) as zf:
                names = zf.namelist()
            util.safe_extract(z, tmp)
            top = names[0].split("/")[0]
            shutil.rmtree(cached, ignore_errors=True)
            os.replace(os.path.join(tmp, top), cached)
            shutil.rmtree(tmp, ignore_errors=True)
            for b in glob.glob(os.path.join(cached, "bin", "*")):
                os.chmod(b, os.stat(b).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            ok("Kotlin compiler provisioned (%s)" % self.kotlin_version(cached))
            return cached
        return self._memoize("kotlin", f)

    def kotlin_jar(self, name):
        """A jar of the Kotlin distribution (kotlin-stdlib.jar, kotlin-reflect.jar, annotations-13.0.jar ...)."""
        h = self.kotlinc_home()
        p = os.path.join(h, "lib", name) if h else None
        return p if p and os.path.isfile(p) else None

    @staticmethod
    def kotlin_version(home):
        try:
            with open(os.path.join(home, "build.txt")) as f:
                return f.read().strip().split()[0]
        except OSError:
            return "?"

    # ------------------------------------------------------------------ KSP (Kotlin Symbol Processing, KSP2 command line)
    def ksp_runner(self):
        """The KSP2 command-line runner: com.google.devtools.ksp.cmdline.KSPJvmMain, which lives in symbol-processing-aa-embeddable.
        -> {"version", "jars": [aa-embeddable, api, common-deps], "poms": [...], "origin"} or None when it is not available.
        Order: APKFORGE_KSP (a folder holding the three jars) -> <project>/vendor and ~/.m2 (Maven layout, the newest release that has all three)
        -> already provisioned -> the release's artifacts.zip from GitHub (pinned)."""
        def flat(d, v, origin):
            jars = [os.path.join(d, "%s-%s.jar" % (n, v)) for n in KSP_ARTIFACTS]
            if not all(os.path.isfile(j) for j in jars):
                return None
            poms = [p for p in (os.path.join(d, "%s-%s.pom" % (n, v)) for n in KSP_ARTIFACTS) if os.path.isfile(p)]
            return {"version": v, "jars": jars, "poms": poms, "origin": origin}

        def f():
            e = os.environ.get("APKFORGE_KSP")
            if e:
                vs = sorted({m.group(1) for n in (os.listdir(e) if os.path.isdir(e) else []) for m in [re.match(r"symbol-processing-aa-embeddable-(.+)\.jar$", n)] if m},
                            key=_vsort, reverse=True)
                for v in vs:
                    r = flat(e, v, "APKFORGE_KSP")
                    if r:
                        return r
                raise ForgeError("APKFORGE_KSP=%s has no complete set: symbol-processing-aa-embeddable-<v>.jar, symbol-processing-api-<v>.jar and "
                                 "symbol-processing-common-deps-<v>.jar of one version" % e)
            for root in [r for r in (self.vendor_dir, os.path.expanduser("~/.m2/repository")) if r]:
                base = os.path.join(root, *(KSP_GROUP.split(".") + [KSP_ARTIFACTS[0]]))
                for v in sorted([d for d in (os.listdir(base) if os.path.isdir(base) else []) if re.match(r"^\d+(\.\d+)*(-\d+(\.\d+)*)?$", d)], key=_vsort, reverse=True):
                    dirs = [os.path.join(root, *(KSP_GROUP.split(".") + [n, v])) for n in KSP_ARTIFACTS]
                    jars = [os.path.join(d, "%s-%s.jar" % (n, v)) for d, n in zip(dirs, KSP_ARTIFACTS)]
                    if all(os.path.isfile(j) for j in jars):
                        poms = [p for p in (os.path.join(d, "%s-%s.pom" % (n, v)) for d, n in zip(dirs, KSP_ARTIFACTS)) if os.path.isfile(p)]
                        return {"version": v, "jars": jars, "poms": poms, "origin": root}
            v, d = KSP_VERSION, self.path("ksp", KSP_VERSION)
            r = flat(d, v, "provisioned")
            if r or not self.fetch_missing:
                return r
            z = fetch(SOURCES["ksp"].format(v=v), self.path("dl", "ksp-artifacts-%s.zip" % v), "ksp-" + v, "KSP %s release artifacts (about 180 MB)" % v, validate=_zip_payload)
            tmp = d + ".tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            os.makedirs(tmp)
            try:
                with zipfile.ZipFile(z) as zf:
                    for n in KSP_ARTIFACTS:
                        for ext in ("jar", "pom"):
                            member = "%s/%s/%s/%s-%s.%s" % (KSP_GROUP.replace(".", "/"), n, v, n, v, ext)
                            try:
                                src = zf.open(member)
                            except KeyError:
                                raise ForgeError("the KSP %s release bundle has no %s" % (v, member))
                            with src, open(os.path.join(tmp, "%s-%s.%s" % (n, v, ext)), "wb") as out:
                                shutil.copyfileobj(src, out)
                shutil.rmtree(d, ignore_errors=True)
                os.replace(tmp, d)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            try:
                os.remove(z)                                           # 180 MB that nothing needs again
            except OSError:
                pass
            ok("KSP %s provisioned (%s)" % (v, ", ".join(KSP_ARTIFACTS)))
            return flat(d, v, "provisioned")
        return self._memoize("ksp", f)

    # ------------------------------------------------------------------ CMake, Ninja, the NDK (native.py)
    def _sdk_cmake_dirs(self):
        sdk = self.sdk()
        return sorted(glob.glob(os.path.join(sdk, "cmake", "*")), key=lambda d: rev_key(os.path.basename(d)), reverse=True) if sdk else []

    def _pypi_tool(self, project, version, name):
        """cmake / ninja from the PyPI wheel for this host: PyPI's JSON API names the files and their sha256 (the publisher's checksum, always enforced; pins.json adds an independent
        pin for the default versions), the wheel is unpacked into <toolchain>/<project>/<version> and the binary is run once.  -> path of the executable."""
        dest = self.path(project, version)
        for c in wheel_binary(dest, name):
            return c
        if not self.fetch_missing:
            raise ForgeError("%s %s is not installed and --offline forbids downloading it. Run `apkforge doctor --native` once with network access, or point apkforge at one: "
                             "APKFORGE_%s=/path/to/%s" % (project, version, project.upper(), name))
        meta = json.loads(util.fetch_text("%s/%s/%s/json" % (PYPI, project, version)) or "{}")
        f = pick_wheel(meta.get("urls", []))
        if not f:
            have = sorted({t for u in meta.get("urls", []) for t in _wheel_tags(u.get("filename", ""))})
            raise ForgeError("PyPI has no %s %s wheel for %s/%s (platform tags it has: %s). Install %s yourself and set APKFORGE_%s=/path/to/%s" % (
                project, version, host_os(), host_arch(), ", ".join(have[:8]) or "none", project, project.upper(), name))
        key = f["filename"][:-4]
        whl = fetch(f["url"], self.path("dl", f["filename"]), key, "%s %s (PyPI wheel, %.1f MB)" % (project, version, f.get("size", 0) / 1e6),
                    sha256=(f.get("digests") or {}).get("sha256"), validate=_zip_payload)
        tmp = dest + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        util.safe_extract(whl, tmp)
        picked = None
        for cand in wheel_binary(tmp, name):
            os.chmod(os.path.dirname(cand), 0o755)
            for fn in os.listdir(os.path.dirname(cand)):
                q = os.path.join(os.path.dirname(cand), fn)
                if os.path.isfile(q):
                    os.chmod(q, os.stat(q).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)         # a zip does not carry the exec bit
            rc, out = run([cand, "--version"], check=False, timeout=60)
            if rc == 0 and out.strip():
                picked = cand
                break
        if not picked:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ForgeError("%s %s was unpacked but its %s does not run on this machine (wheel %s)" % (project, version, name, f["filename"]))
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
        try:
            os.remove(whl)
        except OSError:
            pass
        final = wheel_binary(dest, name)[0]
        ok("%s %s provisioned from PyPI (%s)" % (project, version, f["filename"]))
        return final

    def cmake(self, version=None):
        """-> path of a cmake executable.  APKFORGE_CMAKE=<file> wins.  With `version` (the project's \"native\".\"cmake\".\"version\"): the Android SDK's cmake/<version> when it is
        installed (the one Gradle uses), else that release from PyPI.  Without: CMAKE_VERSION from PyPI (the same on every machine)."""
        def f():
            e = os.environ.get("APKFORGE_CMAKE")
            if e:
                if not os.path.isfile(e):
                    raise ForgeError("APKFORGE_CMAKE=%s is not a file" % e)
                return e
            if version:
                for d in self._sdk_cmake_dirs():
                    b = os.path.basename(d)
                    if (b == version or b.startswith(version + ".")) and os.path.isfile(os.path.join(d, "bin", exe("cmake"))):
                        return os.path.join(d, "bin", exe("cmake"))
            return self._pypi_tool("cmake", version or CMAKE_VERSION, "cmake")
        return self._memoize("cmake:%s" % (version or ""), f)

    def ninja(self):
        """-> path of a ninja executable: APKFORGE_NINJA, the one that the Android SDK's cmake package carries, one already provisioned, else NINJA_VERSION from PyPI."""
        def f():
            e = os.environ.get("APKFORGE_NINJA")
            if e:
                if not os.path.isfile(e):
                    raise ForgeError("APKFORGE_NINJA=%s is not a file" % e)
                return e
            for d in self._sdk_cmake_dirs():
                if os.path.isfile(os.path.join(d, "bin", exe("ninja"))):
                    return os.path.join(d, "bin", exe("ninja"))
            return self._pypi_tool("ninja", NINJA_VERSION, "ninja")
        return self._memoize("ninja", f)

    def tool_version(self, path, pattern):
        """The first match of `pattern` in `path --version` (memoized), or None."""
        def f():
            rc, out = run([path, "--version"], check=False, timeout=60)
            m = re.search(pattern, out) if rc == 0 else None
            return m.group(1) if m else None
        return self._memoize("version:%s" % path, f)

    def cmake_version(self, path):
        v = self.tool_version(path, r"cmake version (\S+)")
        if not v:
            raise ForgeError("%s --version does not say \"cmake version X\": is it a cmake?" % path)
        return v

    def ndk(self, version=None):
        """-> {\"root\", \"revision\", \"origin\"} of an Android NDK.  Order: ANDROID_NDK_ROOT / ANDROID_NDK_HOME / ANDROID_NDK / NDK_ROOT, the Android SDK (ndk/<revision>, ndk-bundle),
        one already provisioned, else the newest stable one from Google's repository manifest (see provision_ndk).  `version`: a revision (\"27.1.12297006\") or its leading
        numbers (\"27\"); an NDK of another revision is skipped, and named in the error when nothing fits."""
        def f():
            tried = []

            def good(d, origin):
                rev = ndk_revision(d)
                if rev and os.path.isfile(os.path.join(d, "build", "cmake", "android.toolchain.cmake")):
                    return {"root": os.path.abspath(d), "revision": rev, "origin": origin}
                return None

            def fits(rev):
                return not version or rev == version or rev.startswith(version + ".") or rev.startswith(version + "-")
            for var in NDK_ENV:
                d = os.environ.get(var)
                if d:
                    got = good(d, var)
                    if got and fits(got["revision"]):
                        return got
                    tried.append("%s=%s (%s)" % (var, d, "NDK " + got["revision"] if got else "not an NDK folder"))
            cands = []
            sdk = self.sdk()
            dirs = ([(d, "Android SDK") for d in glob.glob(os.path.join(sdk, "ndk", "*"))] + [(os.path.join(sdk, "ndk-bundle"), "Android SDK")]) if sdk else []
            dirs += [(d, "provisioned") for d in glob.glob(self.path("ndk", "*"))]
            for d, origin in dirs:
                got = good(d, origin)
                if got and fits(got["revision"]):
                    cands.append(got)
                elif got:
                    tried.append("%s (NDK %s)" % (d, got["revision"]))
            if cands:
                return max(cands, key=lambda c: rev_key(c["revision"]))
            if not self.fetch_missing:
                raise ForgeError("no Android NDK%s found and --offline forbids downloading one%s. Install one (sdkmanager \"ndk;<version>\") and set ANDROID_NDK_ROOT or ANDROID_HOME, "
                                 "or run `apkforge doctor --native` once with network access" % (" " + version if version else "", " (looked at: %s)" % "; ".join(tried) if tried else ""))
            return self.provision_ndk(version, tried)
        return self._memoize("ndk:%s" % (version or ""), f)

    def provision_ndk(self, version=None, tried=()):
        """Download an NDK into <toolchain>/ndk/<revision> from Google's repository manifest (repository2-3.xml): the newest stable package, or the one `version` names, for this host.
        The manifest publishes a sha1 for every archive; it is enforced, and the sha256 is recorded (trust on first use).  Written against manifests of the schema's shape and
        NOT yet run against dl.google.com: the first real run is the test."""
        info("Android NDK: reading %s" % NDK_MANIFEST)
        pkgs = ndk_packages(util.fetch_text(NDK_MANIFEST, timeout=60))
        if version:
            pool = [p for p in pkgs if p["revision"] == version or p["revision"].startswith(version + ".")]
        else:
            pool = [p for p in pkgs if p["stable"]]
        pool = [p for p in pool if pick_ndk_archive(p)]
        if not pool:
            have = sorted({p["revision"] for p in pkgs if pick_ndk_archive(p)}, key=rev_key)
            raise ForgeError("Google's repository manifest has no NDK%s for %s/%s%s. %sSet ANDROID_NDK_ROOT to an NDK you have (Google ships no Linux arm64 NDK)." % (
                " " + version if version else "", host_os(), host_arch(), " (revisions it has: %s)" % ", ".join(have[-6:]) if have else "",
                "Looked at: %s. " % "; ".join(tried) if tried else ""))
        pkg = max(pool, key=lambda p: rev_key(p["revision"]))
        arc = pick_ndk_archive(pkg)
        url = urljoin(NDK_MANIFEST, arc["url"])
        if urlparse(url).scheme != "https":
            raise ForgeError("the SDK repository manifest names a download that is not https: %s" % url)
        info("Android NDK %s: downloading %s (%s). Downloading it means you accept the Android Software Development Kit License Agreement (%s)" % (
            pkg["revision"], os.path.basename(url), util.human(arc["size"]) if arc["size"] else "size unknown", SDK_LICENSE_URL))
        z = fetch(url, self.path("dl", os.path.basename(url)), "ndk-%s-%s" % (pkg["revision"], host_os()), "Android NDK %s" % pkg["revision"], sha1=arc["sha1"], validate=_zip_payload)
        tmp = self.path("ndk.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        extract_zip_unix(z, tmp)
        tops = [d for d in sorted(os.listdir(tmp)) if os.path.isfile(os.path.join(tmp, d, "source.properties"))]
        if len(tops) != 1:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ForgeError("the NDK archive holds %d folder(s) with a source.properties (expected exactly one)" % len(tops))
        got = ndk_revision(os.path.join(tmp, tops[0]))
        if got != pkg["revision"]:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ForgeError("the NDK archive is revision %s, the manifest said %s" % (got, pkg["revision"]))
        dest = self.path("ndk", got)
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(self.path("ndk"), exist_ok=True)
        os.replace(os.path.join(tmp, tops[0]), dest)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.remove(z)                                           # hundreds of MB that nothing needs again
        except OSError:
            pass
        ok("Android NDK %s provisioned: %s" % (got, dest))
        return {"root": dest, "revision": got, "origin": "provisioned"}

    # ------------------------------------------------------------------ bundletool / apksigner (optional)
    def bundletool(self):
        def f():
            e = os.environ.get("APKFORGE_BUNDLETOOL")
            if e and os.path.isfile(e):
                return e
            cached = self.path("bundletool", "bundletool.jar")
            if os.path.isfile(cached):
                return cached
            if not self.fetch_missing:
                return None
            os.makedirs(os.path.dirname(cached), exist_ok=True)
            fetch(SOURCES["bundletool"], cached, "bundletool", "bundletool 1.18.3", validate=_zip_payload)
            return cached
        return self._memoize("bundletool", f)

    def apksigner_jar(self):
        """Google's reference signer/verifier (build-tools). Used only to cross-check this kit's own signer."""
        def f():
            d = self._sdk_build_tools("lib/apksigner.jar")
            if d:
                return os.path.join(d, "lib", "apksigner.jar")
            if not self.fetch_missing or not self.allow_apt:
                return None
            try:
                d = self._apt_extract("apksigner")
            except ForgeError:
                return None
            jars = [j for j in glob.glob(os.path.join(d, "usr/share/java/apksigner*.jar")) if os.path.isfile(j) and not os.path.islink(j)]
            return jars[0] if jars else None
        return self._memoize("apksigner", f)

    def zipalign(self):
        d = self._sdk_build_tools(exe("zipalign"))
        if d:
            return os.path.join(d, exe("zipalign"))
        p = self.path("aapt2", exe("zipalign"))
        return p if os.path.isfile(p) else util.which("zipalign")      # last: one on PATH (what `doctor --allow-apt` installs)

    def downloads_without_pin(self):
        """{url: sha256} of every download that has no entry in pins.json (trust on first use)."""
        pinned = set(PINS.values())
        return {u: s for u, s in tofu_load().items() if s not in pinned}

    # ------------------------------------------------------------------ identity for cache keys
    def ident(self, what):
        """Short stable id of a tool, used in cache keys."""
        paths = {"aapt2": self.aapt2, "dexer": self.dexer, "ecj": self.ecj, "bundletool": self.bundletool}
        if what in paths:
            p = paths[what]()
            return util.sha256_file(p)[:16] if p else "none"
        if what == "javac":
            j = self.javac()
            return util.sha256_bytes(util.tool_output(run([j, "-version"], check=False)[1]).encode())[:16] if j else "none"
        if what == "kotlinc":
            h = self.kotlinc_home()
            return self.kotlin_version(h) if h else "none"
        if what == "ksp":
            r = self.ksp_runner()
            return "%s:%d" % (r["version"], os.path.getsize(r["jars"][0])) if r else "none"
        raise KeyError(what)
