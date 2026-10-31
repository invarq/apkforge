"""NDK / CMake builds (apkforge 1.2.0-dev8): the native half of an Android project.

apkforge.json's "native" setting names a CMakeLists.txt.  For every ABI of the module ("abis") build() does what Gradle's externalNativeBuild does:
  1. configure it with the NDK's own toolchain file (cmake -G Ninja -DCMAKE_TOOLCHAIN_FILE=<ndk>/build/cmake/android.toolchain.cmake -DANDROID_ABI=...),
  2. build the SHARED_LIBRARY / MODULE_LIBRARY targets that CMake's File API (codemodel-v2) names: no scraping of build output, whatever the project's output folders are,
  3. copy each library out of the build tree and strip it with the NDK's llvm-strip (the unstripped one stays in the build tree for symbolication),
  4. add libc++_shared.so from the NDK when the STL is c++_shared,
into ONE tree  <abi>/<lib>.so  that build.py treats like a jniLibs folder.  The APK, the AAB and the AAR of a library module take it from there, so the 16 KB checks (elfcheck.py), the
zip alignment and the verification claims need no second code path.

Choices that differ from Gradle's, each one on purpose:
  * the host's cmake and ninja are not used: CMake and Ninja come from PyPI wheels (toolchain.py), the same version on every machine; "cmakeVersion" asks for another one (the Android SDK's
    cmake/<version> first, then PyPI);
  * -ffile-prefix-map makes the libraries independent of where the project and the NDK live (a __FILE__ in the source, an assert message): the same sources give the same bytes from two directories;
  * a 64-bit library is linked with 16 KB pages (-Wl,-z,max-page-size=16384 -Wl,-z,common-page-size=16384) when the NDK is older than r28, which does it by itself;
  * CC, CXX, CFLAGS, CXXFLAGS, LDFLAGS and the CMAKE_* variables of the caller's environment are removed for the tools: they would change the build from one machine to the next;
  * libc++_shared.so goes into an AAR too (AGP leaves it out and makes the app add it).
Not here (see CHECKLIST Part B): ndk-build, prefab / "prefab" dependencies, IMPORTED libraries (their .so are not packaged), per-variant native settings.
"""
import glob
import hashlib
import json
import os
import re
import shutil
from collections import namedtuple

from . import util
from .toolchain import rev_key
from .util import ForgeError, detail, exe, info, ok, run, warn

ABI_ORDER = ("arm64-v8a", "armeabi-v7a", "x86", "x86_64")           # the ABIs the NDK builds; the order is the order of the work and of the log
TRIPLE = {"arm64-v8a": "aarch64-linux-android", "armeabi-v7a": "arm-linux-androideabi", "x86": "i686-linux-android", "x86_64": "x86_64-linux-android"}
BIT64 = frozenset(("arm64-v8a", "x86_64"))
STLS = ("c++_static", "c++_shared", "none", "system")
STL_SHARED = "libc++_shared.so"
PAGE_FLAGS = ("-Wl,-z,max-page-size=16384", "-Wl,-z,common-page-size=16384")
LINKER_VARS = ("CMAKE_SHARED_LINKER_FLAGS", "CMAKE_MODULE_LINKER_FLAGS", "CMAKE_EXE_LINKER_FLAGS")
CMAKE_PLACES = ("src/main/cpp", "src/main/jni", "cpp", "jni", ".")  # where `"native": true` looks for the top-level CMakeLists.txt
PACKAGED = ("SHARED_LIBRARY", "MODULE_LIBRARY")                     # the CMake target types whose file goes into lib/<abi>/
# The environment that would change a build from one machine to the next (a compiler, flags, a generator, a prefix path): removed for cmake and ninja.
SCRUB_ENV = ("CC", "CXX", "CPP", "ASM", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "ASMFLAGS", "LDFLAGS", "CMAKE_GENERATOR", "CMAKE_GENERATOR_PLATFORM", "CMAKE_GENERATOR_TOOLSET",
             "CMAKE_BUILD_TYPE", "CMAKE_TOOLCHAIN_FILE", "CMAKE_PREFIX_PATH", "CMAKE_MAKE_PROGRAM", "CMAKE_C_COMPILER_LAUNCHER", "CMAKE_CXX_COMPILER_LAUNCHER",
             "CMAKE_CONFIGURATION_TYPES", "CMAKE_EXPORT_COMPILE_COMMANDS", "CMAKE_INSTALL_PREFIX", "CMAKE_FIND_ROOT_PATH", "ANDROID_NDK", "ANDROID_NDK_ROOT", "ANDROID_NDK_HOME",
             "ANDROID_ABI", "ANDROID_PLATFORM", "ANDROID_STL")
FINGERPRINT = ".apkforge-configure.json"

Options = namedtuple("Options", "cmake cmake_version ndk_version arguments c_flags cpp_flags targets stl strip")
# (json key, Options field, kind, default).  kinds: path? = a non-empty string or None; ver? = a version string or None; strs = a list of strings; stl? = one of STLS or None; bool
_SCHEMA = (
    ("cmake", "cmake", "path?", None),
    ("cmakeVersion", "cmake_version", "ver?", None),
    ("ndkVersion", "ndk_version", "ndk?", None),
    ("arguments", "arguments", "strs", []),
    ("cFlags", "c_flags", "strs", []),
    ("cppFlags", "cpp_flags", "strs", []),
    ("targets", "targets", "strs", []),
    ("stl", "stl", "stl?", None),
    ("strip", "strip", "bool", True),
)


# ---------------------------------------------------------------------------------------------------------------- the setting
def parse_option(value, label='"native"'):
    """apkforge.json's "native" -> Options, or None when the hook is off (absent, false).  true = every default (the CMakeLists.txt is looked for in the usual places);
    a string = the path of the CMakeLists.txt (or of its folder); an object = the settings of _SCHEMA.  A typo is an error here, not after the first ABI has been built."""
    if value is None or value is False:
        return None
    if value is True:
        value = {}
    elif isinstance(value, str):
        value = {"cmake": value}
    if not isinstance(value, dict):
        raise ForgeError("%s must be true, false, the path of a CMakeLists.txt or an object, not %s" % (label, json.dumps(value)))
    names = [k for k, _a, _t, _d in _SCHEMA]
    unknown = sorted(set(value) - set(names))
    if unknown:
        raise ForgeError("%s: unknown setting%s %s (allowed: %s)" % (label, "s" if len(unknown) > 1 else "", ", ".join(unknown), ", ".join(names)))
    out = {}
    for key, attr, kind, default in _SCHEMA:
        v = value.get(key, default)
        where = "%s.%s" % (label, key)
        if kind == "bool":
            good, want = isinstance(v, bool), "true or false"
        elif kind == "path?":
            good, want = v is None or (isinstance(v, str) and v.strip() != ""), "a non-empty string"
        elif kind == "ver?":
            good, want = v is None or (isinstance(v, str) and re.fullmatch(r"\d+(\.\d+){0,3}", v) is not None), 'a CMake version such as "3.22.1"'
        elif kind == "ndk?":
            good, want = v is None or (isinstance(v, str) and re.fullmatch(r"\d+(\.\d+){0,2}(-[A-Za-z0-9]+)?", v) is not None), 'an NDK revision such as "27" or "27.1.12297006"'
        elif kind == "stl?":
            good, want = v is None or (isinstance(v, str) and v in STLS), "one of " + ", ".join('"%s"' % s for s in STLS)
        else:                                                              # strs
            good, want = isinstance(v, list) and all(isinstance(x, str) and x.strip() != "" for x in v), "a list of non-empty strings"
        if not good:
            raise ForgeError("%s must be %s, not %s" % (where, want, json.dumps(v)))
        out[attr] = list(v) if isinstance(v, list) else v
    return Options(**out)


def cmake_source(module_dir, opts, label):
    """The top-level CMakeLists.txt of a module, as an absolute path (a folder is taken to mean the CMakeLists.txt in it, as CMake does)."""
    if opts.cmake:
        p = opts.cmake if os.path.isabs(opts.cmake) else os.path.normpath(os.path.join(module_dir, opts.cmake))
        if os.path.isdir(p):
            p = os.path.join(p, "CMakeLists.txt")
        if not os.path.isfile(p):
            raise ForgeError('%s: "native".cmake names %s, which is not a file' % (label, p))
        return p
    for rel in CMAKE_PLACES:
        p = os.path.normpath(os.path.join(module_dir, rel, "CMakeLists.txt"))
        if os.path.isfile(p):
            return p
    raise ForgeError('%s: "native" is on but there is no CMakeLists.txt in %s: set "native": {"cmake": "path/to/CMakeLists.txt"}' % (
        label, ", ".join(os.path.normpath(os.path.join(module_dir, r)) for r in CMAKE_PLACES[:-1] + (".",))))


# ---------------------------------------------------------------------------------------------------------- the arguments
def ndk_major(revision):
    m = re.match(r"(\d+)", revision or "")
    return int(m.group(1)) if m else 0


def effective_stl(opts):
    """The STL the build ends up with: the last -DANDROID_STL=... of "arguments" (CMake: the last -D of a variable wins), else "stl", else the NDK's default, c++_static."""
    stl = opts.stl
    for a in opts.arguments:
        m = re.match(r"^-DANDROID_STL(?::[A-Z]+)?=(.*)$", a)
        if m:
            stl = m.group(1).strip()
    return stl or "c++_static"


def merge_var(args, var, parts):
    """`args` (the -D arguments of the project) with `parts` put IN FRONT of the value of CMake variable `var`, so that the project's own flags are the last word; when no argument sets
    `var`, one is added.  -D<var>=... is a cache entry: a flags variable given on the command line is not extended by CMake's *_INIT, so the merge has to be done here."""
    parts = [p for p in parts if p]
    if not parts:
        return list(args)
    pat = re.compile(r"^-D%s(?::[A-Z]+)?=(.*)$" % re.escape(var), re.S)
    out, found = [], False
    for a in args:
        m = pat.match(a)
        if m:
            found = True
            out.append("-D%s=%s" % (var, " ".join(parts + ([m.group(1).strip()] if m.group(1).strip() else []))))
        else:
            out.append(a)
    if not found:
        out.append("-D%s=%s" % (var, " ".join(parts)))
    return out


def _q(path):
    return '"%s"' % path if re.search(r"\s", path) else path


def prefix_flags(root, ndk_root):
    """-ffile-prefix-map for the project folder and the NDK: __FILE__, assert messages and debug info then do not depend on where either of them is."""
    return ["-ffile-prefix-map=%s" % _q("%s=." % os.path.abspath(root)), "-ffile-prefix-map=%s" % _q("%s=/ndk" % os.path.abspath(ndk_root))]


def page_flags(ndk_revision, abi):
    """The linker flags that make a 64-bit library 16 KB page-size ready, for an NDK that does not do it by itself (r28 and newer do)."""
    return list(PAGE_FLAGS) if abi in BIT64 and 0 < ndk_major(ndk_revision) < 28 else []


def build_type(release):
    """The CMAKE_BUILD_TYPE of the Android Gradle plugin: Debug, and RelWithDebInfo for a release (the symbols are stripped afterwards, into a copy: the unstripped library stays in the build tree)."""
    return "RelWithDebInfo" if release else "Debug"


def defines(ndk_root, ndk_revision, ninja, abi, min_sdk, release, opts, root):
    """Every -D argument of one ABI's configure, in the order they are given.  The project's own "arguments" come last, so that they win; the flags variables are merged (merge_var)."""
    toolchain_file = os.path.join(ndk_root, "build", "cmake", "android.toolchain.cmake")
    base = ["-DANDROID_ABI=%s" % abi, "-DANDROID_PLATFORM=android-%d" % min_sdk, "-DANDROID_NDK=%s" % ndk_root, "-DCMAKE_ANDROID_NDK=%s" % ndk_root,
            "-DCMAKE_TOOLCHAIN_FILE=%s" % toolchain_file, "-DCMAKE_MAKE_PROGRAM=%s" % ninja, "-DCMAKE_BUILD_TYPE=%s" % build_type(release)]
    if opts.stl:
        base.append("-DANDROID_STL=%s" % opts.stl)
    if ndk_major(ndk_revision) == 27:
        base.append("-DANDROID_SUPPORT_FLEXIBLE_PAGE_SIZES=ON")           # r27's own switch for 16 KB pages; r28 does it by default, older NDKs ignore it
    user = list(opts.arguments)
    maps = prefix_flags(root, ndk_root)
    user = merge_var(user, "CMAKE_C_FLAGS", maps + opts.c_flags)
    user = merge_var(user, "CMAKE_CXX_FLAGS", maps + opts.cpp_flags)
    for var in LINKER_VARS:
        user = merge_var(user, var, page_flags(ndk_revision, abi))
    return base + user


def fingerprint(cmake_version, ndk_revision, src, build_dir, argv):
    """What a configured build tree depends on.  CMake keeps every -D of the first configure in CMakeCache.txt, so a changed or removed argument would otherwise leave its old value behind."""
    blob = json.dumps({"cmake": cmake_version, "ndk": ndk_revision, "src": src, "build": build_dir, "argv": argv}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------------------------- File API
def _api_dir(build_dir, *p):
    return os.path.join(build_dir, ".cmake", "api", "v1", *p)


def write_query(build_dir):
    """Ask CMake for the code model (a shared stateless query: an empty file named after the object kind and its major version)."""
    q = _api_dir(build_dir, "query")
    os.makedirs(q, exist_ok=True)
    with open(os.path.join(q, "codemodel-v2"), "w"):
        pass


def read_file_api(build_dir):
    """The targets of the first configuration of CMake's code model -> [{"name", "type", "files": [absolute path of each artifact], "on_disk": nameOnDisk | None}].
    Read after the configure (and again after the build, which reconfigures when a CMakeLists.txt changed)."""
    reply = _api_dir(build_dir, "reply")
    idx = sorted(glob.glob(os.path.join(reply, "index-*.json")))
    if not idx:
        raise ForgeError("CMake wrote no File API reply under %s (apkforge needs CMake 3.14 or newer: leave \"cmakeVersion\" out to get the one apkforge provisions)" % reply)
    try:
        with open(idx[-1], encoding="utf-8") as f:
            index = json.load(f)
        obj = next((o for o in index.get("objects", []) if o.get("kind") == "codemodel"), None)
        if not obj or obj.get("version", {}).get("major") != 2:
            raise ForgeError("CMake's File API reply has no codemodel version 2 (it says: %s)" % json.dumps(obj))
        with open(os.path.join(reply, obj["jsonFile"]), encoding="utf-8") as f:
            model = json.load(f)
        confs = model.get("configurations") or []
        if not confs:
            raise ForgeError("CMake's code model lists no configuration")
        out = []
        for t in confs[0].get("targets", []):
            with open(os.path.join(reply, t["jsonFile"]), encoding="utf-8") as f:
                tj = json.load(f)
            files = []
            for a in tj.get("artifacts", []):
                p = a.get("path", "")
                files.append(p if os.path.isabs(p) else os.path.normpath(os.path.join(build_dir, p)))
            out.append({"name": tj.get("name", t.get("name")), "type": tj.get("type", ""), "files": files, "on_disk": tj.get("nameOnDisk")})
        return out
    except (OSError, ValueError, KeyError) as e:
        raise ForgeError("cannot read CMake's File API reply in %s: %s: %s" % (reply, type(e).__name__, " ".join(str(e).split())[:200]))


def packaged_targets(targets, wanted=()):
    """The targets whose library goes into the APK: every SHARED_LIBRARY / MODULE_LIBRARY, or those named in "targets".  -> (packaged, ignored names)."""
    libs = [t for t in targets if t["type"] in PACKAGED]
    if wanted:
        known = {t["name"] for t in targets}
        missing = [w for w in wanted if w not in known]
        if missing:
            raise ForgeError('"native".targets names %s, which CMake does not define (it defines: %s)' % (", ".join(missing), ", ".join(sorted(known)) or "nothing"))
        notlib = [w for w in wanted if w not in {t["name"] for t in libs}]
        if notlib:
            raise ForgeError('"native".targets names %s, which %s not a shared library (a target is packaged only when it is add_library(<name> SHARED ...) or MODULE)' % (
                ", ".join(notlib), "is" if len(notlib) == 1 else "are"))
        libs = [t for t in libs if t["name"] in wanted]
    ignored = sorted(t["name"] for t in targets if t["type"] == "EXECUTABLE")
    return libs, ignored


# ------------------------------------------------------------------------------------------------------------------- NDK
def ndk_bin(ndk_root, name):
    """<ndk>/toolchains/llvm/prebuilt/<host>/bin/<name>, or None (the host folder is linux-x86_64, darwin-x86_64 or windows-x86_64: the one that is there)."""
    for d in sorted(glob.glob(os.path.join(ndk_root, "toolchains", "llvm", "prebuilt", "*", "bin"))):
        p = os.path.join(d, exe(name))
        if os.path.isfile(p):
            return p
    return None


def ndk_stl_lib(ndk_root, abi):
    """The NDK's libc++_shared.so for `abi`, or None."""
    for d in sorted(glob.glob(os.path.join(ndk_root, "toolchains", "llvm", "prebuilt", "*", "sysroot", "usr", "lib", TRIPLE[abi]))):
        p = os.path.join(d, STL_SHARED)
        if os.path.isfile(p):
            return p
    return None


def _env():
    """what run() is given as `env`: None removes a variable (see util.run)"""
    return {k: None for k in SCRUB_ENV if k in os.environ}


def _wipe(path):
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        raise ForgeError("cannot remove %s (is a process still using it?)" % path)


def _sha256_file(p):
    return util.sha256_file(p)


# ------------------------------------------------------------------------------------------------------------------ build
def cmake_excerpt(text, limit=40):
    """What to show of a failed cmake run: every "CMake Error" block whole.  The first line of a block names the file and the line, the indented lines under it say what is wrong
    ("Cannot find source file: missing.c"), and util.run's default pick (the lines that look like errors) would keep the first and lose the rest.  None when there is no such block
    (a compiler error in ninja's output is picked by the default)."""
    out, inside = [], False
    for line in text.splitlines():
        if re.match(r"CMake Error\b", line):
            inside = True
            out.append(line)
        elif inside and (line.startswith((" ", "\t")) or not line.strip()):
            if line.strip():
                out.append(line.rstrip())
        else:
            inside = False
    return out[:limit] if out else None


def _configure_and_build(tc, cmake, cmake_version, ninja, ndk, abi, src, bdir, min_sdk, release, opts, root, wanted):
    """One ABI: configure (only when the tree is new or its settings changed), build the shared libraries, -> [(target, path of the built library)]."""
    argv = defines(ndk["root"], ndk["revision"], ninja, abi, min_sdk, release, opts, root)
    fp = fingerprint(cmake_version, ndk["revision"], src, bdir, argv)
    fp_file = os.path.join(bdir, FINGERPRINT)
    old = None
    try:
        with open(fp_file, encoding="utf-8") as f:
            old = json.load(f).get("fingerprint")
    except (OSError, ValueError):
        pass
    if old != fp:
        if os.path.exists(bdir):
            detail("native %s: the settings changed (or this is a new tree): configuring from scratch" % abi)
        _wipe(bdir)
        os.makedirs(bdir)
        write_query(bdir)
        cmd = [cmake, "-S", os.path.dirname(src), "-B", bdir, "-G", "Ninja", "--no-warn-unused-cli"] + argv                 # -S / -B: CMake 3.13 and newer, and 3.14 is the least build() accepts
        run(cmd, cwd=bdir, env=_env(), what="cmake configure (%s)" % abi, excerpt=cmake_excerpt)
        with open(fp_file, "w", encoding="utf-8") as f:
            json.dump({"fingerprint": fp, "argv": argv}, f, indent=1, sort_keys=True)
    targets = read_file_api(bdir)
    libs, ignored = packaged_targets(targets, wanted)
    if not libs:
        seen = ", ".join("%s (%s)" % (t["name"], t["type"].lower().replace("_", " ")) for t in targets) or "no target at all"
        raise ForgeError("CMake builds no shared library for %s: nothing could go into lib/%s/ (CMake defines: %s). "
                         "Declare one with add_library(<name> SHARED <sources>)." % (src, abi, seen))
    if ignored:
        detail("native %s: executable target(s) %s are built by CMake but not packaged (an APK carries libraries)" % (abi, ", ".join(ignored)))
    names = [t["name"] for t in libs]
    if rev_key(cmake_version)[0] >= [3, 15]:
        run([cmake, "--build", bdir, "--target"] + names, cwd=bdir, env=_env(), what="cmake --build (%s)" % abi, excerpt=cmake_excerpt)
    else:                                                                    # CMake 3.14 builds one target per --build
        for n in names:
            run([cmake, "--build", bdir, "--target", n], cwd=bdir, env=_env(), what="cmake --build %s (%s)" % (n, abi), excerpt=cmake_excerpt)
    libs, _ = packaged_targets(read_file_api(bdir), wanted)                  # the build may have reconfigured: what the code model says NOW
    out = []
    for t in libs:
        built = [f for f in t["files"] if f.endswith(".so")]
        if not built or not os.path.isfile(built[0]):
            raise ForgeError("CMake target %s (%s) was built but its library is not on disk: %s" % (t["name"], abi, built[0] if built else "the code model names no .so file"))
        out.append((t["name"], built[0]))
    return out


def build(tc, opts, module_dir, root, work, release, abis, min_sdk, label):
    """Build the native code of one module for every ABI that both the module ("abis") and the NDK know.  `work` is a folder of the module's own for the build trees and the result.
    -> {"dir": the <abi>/<lib>.so tree, "libs": [{"abi", "name", "target", "size", "sha256", "stripped"}], "abis", "stl", "ndk": {"revision", "origin"}, "cmake", "ninja", "buildType"}"""
    src = cmake_source(module_dir, opts, label)
    cmake = tc.cmake(opts.cmake_version)
    cmake_version = tc.cmake_version(cmake)
    if rev_key(cmake_version)[0] < [3, 14]:
        raise ForgeError("%s: CMake %s is too old: apkforge reads CMake's File API (3.14 and newer) to learn which libraries a build made. "
                         "Leave \"cmakeVersion\" out, and apkforge provisions CMake %s" % (label, cmake_version, "3.31"))
    ninja = tc.ninja()
    ninja_version = tc.tool_version(ninja, r"(\d+(?:\.\d+)+)") or "?"
    ndk = tc.ndk(opts.ndk_version)
    todo = [a for a in ABI_ORDER if a in abis]
    skipped = [a for a in abis if a not in ABI_ORDER]
    if skipped:
        warn("%s: no NDK build for ABI %s (the NDK builds %s): prebuilt libraries of those ABIs under jniLibs/ are still packaged" % (label, ", ".join(skipped), ", ".join(ABI_ORDER)))
    if not todo:
        raise ForgeError('%s: "abis" (%s) has none of the ABIs the NDK builds (%s)' % (label, ", ".join(abis) or "empty", ", ".join(ABI_ORDER)))
    stl = effective_stl(opts)
    stripper = ndk_bin(ndk["root"], "llvm-strip") if opts.strip else None
    if opts.strip and not stripper:
        warn("%s: the NDK has no llvm-strip (%s): the native libraries are packaged unstripped" % (label, ndk["root"]))
    info("native %s: CMake %s, Ninja %s, NDK %s (%s), %s, ABIs %s" % (label, cmake_version, ninja_version, ndk["revision"], ndk["origin"], stl, ", ".join(todo)))
    outdir = os.path.join(work, "libs")
    _wipe(outdir)
    libs, names_seen = [], {}
    for abi in todo:
        built = _configure_and_build(tc, cmake, cmake_version, ninja, ndk, abi, src, os.path.join(work, "cmake", abi), min_sdk, release, opts, root, opts.targets)
        os.makedirs(os.path.join(outdir, abi), exist_ok=True)
        for target, path in built:
            name = os.path.basename(path)
            dst = os.path.join(outdir, abi, name)
            if os.path.exists(dst):
                raise ForgeError("%s: two CMake targets make lib/%s/%s (%s and %s)" % (label, abi, name, names_seen[(abi, name)], target))
            names_seen[(abi, name)] = target
            did = False
            if stripper:
                run([stripper, "--strip-unneeded", "-o", dst, path], what="llvm-strip %s (%s)" % (name, abi))
                did = True
            else:
                shutil.copyfile(path, dst)
            libs.append({"abi": abi, "name": name, "target": target, "stripped": did})
        if stl == "c++_shared" and not os.path.exists(os.path.join(outdir, abi, STL_SHARED)):
            shared = ndk_stl_lib(ndk["root"], abi)
            if not shared:
                raise ForgeError("the STL is c++_shared but the NDK has no %s for %s (looked in %s/toolchains/llvm/prebuilt/*/sysroot/usr/lib/%s)" % (STL_SHARED, abi, ndk["root"], TRIPLE[abi]))
            shutil.copyfile(shared, os.path.join(outdir, abi, STL_SHARED))
            libs.append({"abi": abi, "name": STL_SHARED, "target": "(NDK)", "stripped": True})
        ok("native %s %s: %s" % (label, abi, ", ".join(l["name"] for l in libs if l["abi"] == abi)))
    for l in libs:
        p = os.path.join(outdir, l["abi"], l["name"])
        l["size"], l["sha256"] = os.path.getsize(p), _sha256_file(p)
    return {"dir": outdir, "libs": libs, "abis": todo, "stl": stl, "ndk": {"revision": ndk["revision"], "origin": ndk["origin"]},
            "cmake": cmake_version, "ninja": ninja_version, "buildType": build_type(release)}


def summary(res):
    """what `build --json` shows of a native build (no path of this machine in it)"""
    return {"abis": res["abis"], "stl": res["stl"], "ndk": res["ndk"], "cmake": res["cmake"], "ninja": res["ninja"], "buildType": res["buildType"],
            "libs": [{k: l[k] for k in ("abi", "name", "target", "size", "sha256", "stripped")} for l in res["libs"]]}
