"""apkforge command line."""
import argparse
import json
import os
import sys
import time
import zipfile

from . import project, signing, templates, util
from .util import ForgeError, info, ok, warn


def _toolchain(a):
    from . import toolchain as T
    if getattr(a, "trust_new", False):
        T.TRUST_NEW[0] = True
    allow_apt = getattr(a, "allow_apt", False) or os.environ.get("APKFORGE_ALLOW_APT", "").lower() in ("1", "true", "yes")
    offline = getattr(a, "offline", False) or util.env_offline()
    util.set_offline(offline)                                  # enforced inside util.download / fetch_text / redirect_location
    return T.Toolchain(fetch_missing=not offline, allow_apt=allow_apt)


def _load(a):
    proj = project.load(a.path)
    util.set_log(os.path.join(proj.root, ".apkforge", "build.log"), verbose=getattr(a, "verbose", False))
    return proj


def cmd_build(a):
    from .build import Builder
    if a.no_sign_aab and not a.aab:
        raise ForgeError("--no-sign-aab only matters together with --aab")
    if a.json:
        util.log_to_stderr()
    proj = _load(a)
    b = Builder(proj, _toolchain(a), release=a.release, offline=util.is_offline(), v4=a.v4, optimize=a.optimize, react=a.react, sign_aab=not a.no_sign_aab)
    t0 = time.time()
    res = b.build(a.module, aab=a.aab)
    if a.json:
        print(json.dumps({k: v for k, v in res.items()}, indent=1, default=str))
    info("done in %.1fs" % (time.time() - t0))
    return 0


def cmd_run(a):
    """Build the app, install it on one device over adb, start its launcher activity and, with --logcat, follow the app's log."""
    from . import adb as A
    from . import build as B
    from . import manifest as M
    from .build import Builder
    proj = _load(a)
    path, found_by = A.find_adb(a.adb)
    dev = A.pick_device(A.devices(path), a.serial, os.environ.get("ANDROID_SERIAL"))      # before the build: a build that cannot be installed is minutes lost
    adb = A.Adb(path, dev["serial"])
    info("adb %s (%s); device %s" % (path, found_by, A.device_label(dev)))
    b = Builder(proj, _toolchain(a), release=a.release, offline=util.is_offline(), v4=a.v4, optimize=a.optimize, react=a.react)
    res = b.build(a.module)
    apk = res.get("apk") if isinstance(res, dict) else None
    if not apk:
        raise ForgeError("run needs an app module, but this build produced a library (choose the app with --module)")
    appid, entries = res["application_id"], res["launchers"]
    if a.activity:
        chosen = {"name": M.absolute_name(a.activity, res["namespace"]), "category": "LAUNCHER", "explicit": True}
    else:
        chosen = M.pick_launcher(entries)
        if not chosen:
            raise ForgeError("the merged manifest declares no launcher activity (an enabled <activity> or <activity-alias> with an <intent-filter> that has the action MAIN "
                             "and the category LAUNCHER).  Add one, or start another activity with --activity NAME")
        names = sorted({e["name"] for e in entries})
        if len(names) > 1:
            info("%d launcher activities (%s); starting %s (--activity chooses another)" % (len(names), ", ".join(names), chosen["name"]))
    try:
        aapt2 = b.tc.aapt2()
    except ForgeError:
        aapt2 = None
    package, root = B.read_apk_manifest(aapt2, apk)
    ok("checked: " + B.render_claims(B.launch_claims(appid, entries, chosen, package, root)))
    t0 = time.time()
    if res.get("idsig") and not a.no_incremental:
        info("%s lies beside the APK: adb installs incrementally when the device supports it, and keeps streaming the APK to the device after the install reports done "
             "(stay connected; --no-incremental installs the whole file first)" % os.path.basename(res["idsig"]))
    how = A.install(adb, apk, appid, reinstall=a.reinstall, no_incremental=a.no_incremental)
    ok("%s %s on %s (%s, %.1fs)" % (how, os.path.relpath(apk, proj.root), dev["serial"], util.human(res["size"]), time.time() - t0))
    started = A.start(adb, appid, chosen["name"], chosen["category"])
    ok("started %s: %s" % (started["component"], A.describe_start(started)))
    return A.follow_log(adb, appid) if a.logcat else 0


def cmd_publish(a):
    from .build import Builder
    proj = _load(a)
    b = Builder(proj, _toolchain(a), release=a.release, offline=util.is_offline())
    b.publish_local(a.module, os.path.abspath(a.repo), a.group, a.version)
    return 0


def cmd_vendor(a):
    from .build import Builder
    proj = _load(a)
    b = Builder(proj, _toolchain(a), release=False, offline=util.is_offline())
    n, total, vdir = b.vendor(a.module)
    ok("vendored %d new file(s) (%s); vendor/ now holds %d files and is locked in vendor/apkforge.lock.json" % (n, util.human(total), len(b.vendor_lock)))
    info("commit vendor/ with the project: builds find it first and can run with --offline")
    return 0


def cmd_clean(a):
    from .build import Builder
    proj = project.load(a.path)
    Builder(proj, _toolchain(a)).clean()
    ok("cleaned %s" % proj.root)
    return 0


def cmd_new(a):
    root, pkg = templates.create(a.dir, a.template, a.name, a.package)
    ok("created %s project in %s (package %s)" % (a.template, root, pkg))
    if pkg.startswith("com.example."):
        info('note:  Google Play rejects com.example.* packages: set "applicationId" in apkforge.json before you publish (or create the project with --package your.id)')
    info("next:  apkforge build %s     (release: apkforge keygen %s, then apkforge build %s --release)" % (a.dir, a.dir, a.dir))
    return 0


def _find_keytool(tc):
    """keytool from PATH, else the one beside the JDK apkforge builds with (provisioned if it has to be)"""
    kt = util.which("keytool")
    if kt:
        return kt
    for get in (tc.javac, tc.ensure_javac):
        try:
            j = get()
        except ForgeError:
            j = None
        cand = os.path.join(os.path.dirname(j), "keytool.exe" if util.host_os() == "windows" else "keytool") if j else None
        if cand and os.path.isfile(cand):
            return cand
    return None


def _gitignore_keystore(root, ks):
    gi = os.path.join(root, ".gitignore")
    ks = os.path.abspath(ks)
    if not os.path.isfile(gi) or not ks.startswith(root + os.sep):
        return
    rel = os.path.relpath(ks, root).replace(os.sep, "/")
    with open(gi, encoding="utf-8") as f:
        text = f.read()
    have = [l.strip() for l in text.splitlines()]
    if rel in have or "*" + os.path.splitext(rel)[1] in have:
        return
    with open(gi, "a", encoding="utf-8") as f:
        f.write(("" if not text or text.endswith("\n") else "\n") + rel + "\n")
    info("added %s to .gitignore (a signing key must not be committed)" % rel)


def cmd_keygen(a):
    """Create a release keystore and write the matching "signing" section in one step.  With --upload: an upload key for the Android App Bundle and the "uploadSigning" section."""
    import secrets
    util.set_log(None, verbose=a.verbose)
    section = "uploadSigning" if a.upload else "signing"
    password_env = a.password_env or ("APKFORGE_UPLOAD_PASS" if a.upload else "APKFORGE_STORE_PASS")
    proj = project.load(a.path)
    apps = [m for m in proj.modules.values() if m.is_app]
    if len(apps) != 1:
        raise ForgeError('keygen needs a project with exactly one app module (this one has %d): add the "%s" section by hand' % (len(apps), section))
    m = apps[0]
    cfg_file = os.path.join(proj.root, "apkforge.json")
    with open(cfg_file, encoding="utf-8") as f:
        cfg = json.load(f)
    mcfg = next(v for k, v in cfg["modules"].items() if ":" + k.lstrip(":") == m.name) if "modules" in cfg else cfg
    if (mcfg.get(section) or cfg.get(section)) and not a.force:
        raise ForgeError('apkforge.json already has a "%s" section; keygen will not replace it (--force writes a new section; the keystore file itself is never overwritten)' % section)
    ks = os.path.abspath(a.keystore) if a.keystore else os.path.join(proj.root, "upload.p12" if a.upload else "release.p12")
    pw = os.environ.get(password_env)
    shown = not pw
    pw = pw or secrets.token_urlsafe(18)
    alias = a.alias or ("upload" if a.upload else "release")
    dname = a.dname or "CN=" + (m.application_id or os.path.basename(proj.root))
    tc = _toolchain(a)
    kt = _find_keytool(tc)
    if not kt:
        raise ForgeError("keytool not found: install a JDK, or run  apkforge doctor  to provision the one apkforge builds with")
    fp = signing.generate_keystore(ks, alias, pw, dname, kt)
    ok("created %s (alias %s, RSA 2048, valid about 27 years); certificate sha256 %s" % (os.path.relpath(ks), alias, fp))
    mcfg[section] = {"keystore": os.path.relpath(ks, m.dir).replace(os.sep, "/"), "alias": alias, "storePasswordEnv": password_env}
    with open(cfg_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, indent=2) + "\n")
    ok('wrote the "%s" section into %s (the password is read from $%s and is not stored in the project)' % (section, cfg_file, password_env))
    _gitignore_keystore(proj.root, ks)
    if shown:
        util.secret("password (shown once, stored nowhere): %s" % pw)
    info("next:  read -rs %s; export %s; apkforge build %s --release%s     (typing it keeps the password out of your shell history)" % (
        password_env, password_env, a.path, " --aab" if a.upload else ""))
    if a.upload:
        info("keep %s and its password safe: Google Play checks every upload against this key" % os.path.basename(ks))
    else:
        info("keep %s and its password safe: an app can only be updated with the key it was first signed with" % os.path.basename(ks))
    return 0


def cmd_doctor(a):
    util.set_log(None, verbose=a.verbose)
    tc = _toolchain(a)
    rows = []
    print("  %-16s %s" % ("apt-get", "allowed (--allow-apt)" if tc.allow_apt else "not used: --offline" if tc.apt_requested else "never used (opt in with --allow-apt or APKFORGE_ALLOW_APT=1)"))
    steps = [("aapt2", tc.aapt2), ("D8/R8", tc.dexer), ("android.jar 35", lambda: tc.android_jar(35)), ("javac", tc.ensure_javac)]
    if a.kotlin:
        steps.append(("kotlinc", tc.kotlinc_home))
    if a.ksp:
        steps.append(("ksp", lambda: (lambda r: "%s  %s" % (r["version"], r["jars"][0]) if r else None)(tc.ksp_runner())))
    if a.bundletool:
        steps.append(("bundletool", tc.bundletool))
    if a.native:                                              # what a module with a "native" setting needs: CMake, Ninja, an NDK
        steps.append(("cmake", lambda: (lambda p: "%s  %s" % (tc.cmake_version(p), p))(tc.cmake())))
        steps.append(("ninja", lambda: (lambda p: "%s  %s" % (tc.tool_version(p, r"(\d+(?:\.\d+)+)") or "?", p))(tc.ninja())))
        steps.append(("NDK", lambda: (lambda n: "%s  %s (%s)" % (n["revision"], n["root"], n["origin"]))(tc.ndk())))
    bad = 0
    for name, fn in steps:
        try:
            p = fn()
            rows.append((name, p or "-- not available"))
            bad += 0 if p else 1
        except ForgeError as e:
            rows.append((name, "FAILED: %s" % e))
            bad += 1
    for n, p in rows:
        print("  %-16s %s" % (n, p))
    try:
        found = {k: bool(v) for k, v in tc.oracles().items()}
        print("  %-16s %s" % ("oracles", found))
        if not all(found.values()) and not tc.allow_apt:
            print("  %-16s a missing verifier leaves its claims [internal only]; `apkforge doctor --allow-apt` installs apksigner, zipalign, dexdump and readelf (binutils) with apt-get" % "")
    except ForgeError as e:
        print("  %-16s unavailable: %s" % ("oracles", str(e).splitlines()[0]))
    try:
        print("  %-16s aapt2 %s | d8 %s" % ("versions", tc.aapt2_version(), tc.dexer_version()[:12]))
    except ForgeError as e:
        print("  %-16s unavailable: %s" % ("versions", str(e).splitlines()[0]))
    loose = tc.downloads_without_pin()
    if loose:
        print("  %-16s %d download(s) are not in pins.json; their checksums are recorded on first use (trust on first use):" % ("pins", len(loose)))
        for u, s in sorted(loose.items()):
            print("      %s  %s" % (s[:16], u))
    print("toolchain ready" if not bad else "%d tool(s) missing" % bad)
    return 1 if bad else 0


def _is_bundle(path):
    """an Android App Bundle: named .aab, or a zip that holds BundleConfig.pb"""
    if path.lower().endswith(".aab"):
        return True
    try:
        with zipfile.ZipFile(path) as z:
            return "BundleConfig.pb" in z.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def cmd_verify(a):
    """`verify` takes an APK (cmd_verify_apk) or an Android App Bundle (cmd_verify_bundle)."""
    if _is_bundle(a.apk):
        return cmd_verify_bundle(a)
    if a.play or a.allow_unsigned:
        raise ForgeError("--play and --allow-unsigned are for Android App Bundles (.aab); %s is not one" % a.apk)
    return cmd_verify_apk(a)


def cmd_verify_bundle(a):
    """apkforge's own readers of an Android App Bundle (JAR signature, proto manifest, BundleConfig.pb), plus jarsigner and bundletool when they are installed: never the network, never
    apt.  JSON report on stdout and the log on stderr, like `verify` of an APK; exit status 1 when the signature or the structure is wrong.  What Google Play will object to is listed
    under "play" and logged; --play makes those errors fail the command too."""
    from . import build as B
    from . import toolchain as T
    util.log_to_stderr()                      # stdout is the JSON report and nothing else
    if not os.path.isfile(a.apk):
        raise ForgeError("no such file: %s" % a.apk)
    tc = T.Toolchain(fetch_missing=False, allow_apt=False)
    rep = B.verify_bundle(tc, a.apk, unsigned_ok=a.allow_unsigned)
    play, problems = rep["play"], list(rep["problems"])
    for w in rep["signature"]["warnings"]:
        warn("bundle signature: " + w)
    for e in play["errors"]:
        if a.play:
            problems.append("Google Play: " + e)
        else:
            warn("Google Play would refuse this bundle: " + e)
    for w in play["warnings"]:
        warn("Google Play: " + w)
    print(json.dumps(dict(rep["signature"], problems=problems, bundle=rep["bundle"], play=play, oracles=rep["oracles"], claims=rep["claims"]), indent=1, default=str))
    if problems:
        raise ForgeError("verification failed: " + "; ".join(p.splitlines()[0] for p in problems))
    ok("verified: " + B.render_claims(rep["claims"]))
    return 0


def cmd_test_bundle(a):
    """Try a bundle out on this machine, the way Google Play would hand it out: build it (or take a .aab that exists), have bundletool make the universal APK from it, sign that APK with
    the normal "signing" / debug key, check it, and with --install put it on a device over adb and start the launcher activity."""
    import shutil
    import tempfile
    from . import adb as A
    from . import build as B
    from . import manifest as M
    from .build import Builder
    if a.json:
        util.log_to_stderr()
    if not a.install:
        given = [flag for flag, v in (("--serial", a.serial), ("--adb", a.adb), ("--reinstall", a.reinstall), ("--logcat", a.logcat)) if v]
        if given:
            raise ForgeError("without --install there is no device for %s" % ", ".join(given))
    standalone = os.path.isfile(a.path)
    if standalone and a.release:
        raise ForgeError("--release builds a bundle; a .aab that exists is tested as it is")
    adb = dev = None
    if a.install:                                              # before the build: a bundle that cannot be installed is minutes lost
        path, found_by = A.find_adb(a.adb)
        dev = A.pick_device(A.devices(path), a.serial, os.environ.get("ANDROID_SERIAL"))
        adb = A.Adb(path, dev["serial"])
        info("adb %s (%s); device %s" % (path, found_by, A.device_label(dev)))
    t0 = time.time()
    scratch = None
    if standalone:
        util.set_log(None, verbose=a.verbose)
        tc, aab_path, min_sdk = _toolchain(a), os.path.abspath(a.path), None
        signer = signing.debug_signer(util.home_dir())
        scratch = work = tempfile.mkdtemp(prefix="apkforge-test-bundle-")
        info("%s: tried out as it is; its universal APK is signed with the debug key" % os.path.basename(aab_path))
    else:
        proj = _load(a)
        b = Builder(proj, _toolchain(a), release=a.release, offline=util.is_offline(), optimize=a.optimize, react=a.react)
        res = b.build(a.module, aab=True)
        if not res.get("aab"):
            raise ForgeError("test-bundle needs an app module, but this build produced a library (choose the app with --module)")
        m = proj.module(a.module) if a.module else proj.default_app()
        tc, aab_path, signer, min_sdk, work = b.tc, res["aab"], b.signers[m.name], m.minSdk, b.work(m, "test-bundle")
    apk_path = (aab_path[:-4] if aab_path.lower().endswith(".aab") else aab_path) + "-universal.apk"
    try:
        chk = B.universal_check(tc, aab_path, apk_path, signer, min_sdk, work)
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    ok("universal APK  %s  (%s)  sha256 %s" % (os.path.relpath(chk["apk"]), util.human(chk["size"]), chk["sha256"][:16]))
    ok("checked: " + B.render_claims(chk["claims"]))
    installed = None
    if a.install:
        chosen = M.pick_launcher(chk["launchers"])
        if not chosen:
            raise ForgeError("the bundle declares no launcher activity, so there is nothing to start (the universal APK is %s)" % chk["apk"])
        t1 = time.time()
        how = A.install(adb, chk["apk"], chk["package"], reinstall=a.reinstall, no_incremental=False)
        ok("%s %s on %s (%s, %.1fs)" % (how, os.path.basename(chk["apk"]), dev["serial"], util.human(chk["size"]), time.time() - t1))
        started = A.start(adb, chk["package"], chosen["name"], chosen["category"])
        ok("started %s: %s" % (started["component"], A.describe_start(started)))
        installed = {"serial": dev["serial"], "component": started["component"]}
    if a.json:
        print(json.dumps({"aab": aab_path, "apk": chk["apk"], "size": chk["size"], "sha256": chk["sha256"], "package": chk["package"], "claims": chk["claims"], "installed": installed},
                         indent=1, default=str))
    info("done in %.1fs" % (time.time() - t0))
    return A.follow_log(adb, chk["package"], out=sys.stderr if a.json else None) if a.install and a.logcat else 0


def cmd_verify_apk(a):
    """apkforge's own verifier plus every external verifier that is already installed (apksigner and readelf on PATH, zipalign from the toolchain): never the network,
    never apt. The claims say which check was whose; the exit status is 1 when anything rejects the APK."""
    from . import build as B
    from . import elfcheck
    from . import toolchain as T
    util.log_to_stderr()                      # stdout is the JSON report and nothing else
    rep = signing.verify_apk(a.apk)
    tc = T.Toolchain(fetch_missing=False, allow_apt=False)
    found = tc.oracles() if os.path.isfile(a.apk) else None
    ora = B.run_apk_oracles(tc, a.apk, None, True, found) if found is not None else None
    elf = B.elf_report(found, a.apk) if found is not None and a.elf != "off" else None
    problems = list(rep["problems"]) + (B.oracle_rejections(ora) if ora else [])
    if elf:
        for w in elf["warnings"]:
            warn(w)
        problems += elf["errors"]
        if a.elf == "error":
            problems += elf["rule"]
        else:
            for line in elf["rule"]:
                warn("16 KB page size: " + line)
    claims = [] if (rep["problems"] or not ora) else B.signature_claims(rep, ora) + [B.alignment_claim(ora)] + (elf["claims"] if elf else [])
    out = dict(rep, problems=problems, claims=claims)
    if elf and elf["libs"]:
        out["native_libs"] = elfcheck.strip(elf["libs"])
    print(json.dumps(out, indent=1, default=str))
    if problems:
        raise ForgeError("verification failed: " + "; ".join(p.splitlines()[0] for p in problems))
    ok("verified: " + B.render_claims(claims))
    return 0


def _import_summary(out, verbose):
    """what the importer did with every statement it read: its own counts, never hidden (the classes must add up to the total)"""
    st = out.get("stats")
    if not st:
        return None
    rest = st.get("structural", 0) + st.get("covered", 0)
    total = st.get("total", 0)
    line = "import-gradle: %d statements read: %d mapped, %d ignored on purpose, %d not mapped, %d structural or covered by a parent" % (
        total, st.get("mapped", 0), st.get("ignored", 0), st.get("unmapped", 0), rest)
    lost = total - (st.get("mapped", 0) + st.get("ignored", 0) + st.get("unmapped", 0) + rest)
    if lost:
        line += ", %d NOT CLASSIFIED" % lost
    if st.get("ignored", 0) and not verbose:
        line += "; -v lists the ignored ones"
    return line


def cmd_import_gradle(a):
    from . import gradle_import
    util.log_to_stderr()                      # stdout is the JSON config and nothing else
    out = gradle_import.import_project(a.path, write=not a.dry_run, force=a.force)
    print(json.dumps(out["config"], indent=2))
    for n in out["notes"]:
        warn(n)
    line = _import_summary(out, a.verbose)
    if line:
        info(line)
    if a.verbose:
        for e in out.get("ignored", []):
            info("IGNORED " + e)
    if not a.dry_run:
        ok("wrote %s" % out["file"])
    return 0


QUICKSTART = """quick start:
  apkforge new myapp --package com.acme.myapp      a project that builds as-is (API 36, minSdk 24)
  apkforge build myapp                             debug APK, checked after the build -> myapp/build/outputs/
  apkforge run myapp                               build, install on the connected device or emulator (adb), start the launcher activity; --logcat follows its log
  apkforge keygen myapp                            release keystore + "signing" section (the password is printed once)
  read -rs APKFORGE_STORE_PASS; export APKFORGE_STORE_PASS     type the password; it stays out of your shell history
  apkforge build myapp --release
  apkforge verify myapp/build/outputs/*.apk        JSON report on stdout, log on stderr; exit status 1 when anything rejects the APK
  apkforge keygen myapp --upload                   optional: a separate upload key and the "uploadSigning" section (without it the bundle is signed with "signing")
  apkforge build myapp --release --aab             also an Android App Bundle: signed, checked, and what Google Play will object to is printed
  apkforge verify myapp/build/outputs/*.aab        the same kind of check for a bundle; --play makes what Google Play objects to a failure
  apkforge test-bundle myapp [--install]           bundletool's universal APK of the bundle, signed and checked; --install runs it on a device
  apkforge doctor [--allow-apt]                    provision / check the toolchain; --allow-apt also installs apksigner, zipalign, dexdump so
                                                   that signature and alignment claims are [confirmed] by Google's tools, not [internal only]
"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="apkforge", description="Gradle-free Android build system (aapt2 + javac/kotlinc + D8/R8 + pure-Python signing).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=QUICKSTART)
    ap.add_argument("--version", action="version", version="apkforge " + util.VERSION + (" (%s)" % util.BUILD if util.BUILD else ""))
    sub = ap.add_subparsers(dest="cmd")

    def common(p, path=True):
        if path:
            p.add_argument("path", nargs="?", default=".", help="project directory (default: .)")
        p.add_argument("-v", "--verbose", action="store_true", help="show cache hits and rebuild reasons")
        p.add_argument("--offline", action="store_true", help="never touch the network (toolchain, dependencies, apt); also APKFORGE_OFFLINE=1")
        p.add_argument("--allow-apt", action="store_true", help="allow apt-get (system-wide package installs as root) when a tool is missing; also APKFORGE_ALLOW_APT=1")
        p.add_argument("--trust-new", action="store_true", help="accept a download whose checksum differs from its pin / earlier download (both checksums are printed)")

    def v4_flags(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--v4", dest="v4", action="store_const", const=True, default=None,
                       help='also write <apk>.idsig, the APK Signature Scheme v4 file that lets adb install incrementally (Android 11+); default: "v4Signing" in apkforge.json, which is off')
        g.add_argument("--no-v4", dest="v4", action="store_const", const=False, help='no <apk>.idsig for this build, whatever "v4Signing" says')

    def optimize_flags(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--optimize-resources", dest="optimize", action="store_const", const=True, default=None,
                       help='release builds: run aapt2 optimize on the resources (shorter res/ paths; sparse tables from minSdk 32; resource NAMES are collapsed only with '
                            '"optimizeResources": {"collapseNames": true, "keep": [...]}); default: "optimizeResources" in apkforge.json, which is off')
        g.add_argument("--no-optimize-resources", dest="optimize", action="store_const", const=False, help='no resource optimization for this build, whatever "optimizeResources" says')

    def react_flags(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--react", dest="react", action="store_const", const=True, default=None,
                       help='a React Native app: non-debug builds bundle the JavaScript with Metro and compile it to Hermes bytecode (see "react" in apkforge.json for the settings); '
                            'default: "react" in apkforge.json, which is off')
        g.add_argument("--no-react", dest="react", action="store_const", const=False, help='no JavaScript bundle for this build, whatever "react" says (the APK then has no JavaScript)')

    p = sub.add_parser("build", help="build an APK (or AAR for a library module)")
    common(p)
    p.add_argument("--release", action="store_true")
    p.add_argument("--aab", action="store_true", help='also produce an Android App Bundle: signed with "uploadSigning" (release builds; else "signing", else the debug key) and checked')
    p.add_argument("--no-sign-aab", action="store_true", help="leave the bundle unsigned (it is signed somewhere else, for example by a CI step)")
    v4_flags(p)
    optimize_flags(p)
    react_flags(p)
    p.add_argument("--module", "-m", help="module to build (e.g. :app)")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("run", help="build, install on a device or emulator over adb, and start the app",
                       epilog="adb is looked for on the PATH, then in $ANDROID_HOME/platform-tools (or pass --adb).  One connected device is used as it is; with several, choose -s SERIAL "
                              "(or ANDROID_SERIAL).  The launcher activity is the one the home screen would start; it and the applicationId are checked against aapt2's reading of the "
                              "APK and printed with their tag ([internal + aapt2] = both agree).  The adb steps themselves are not independently verified.")
    common(p)
    p.add_argument("--release", action="store_true")
    p.add_argument("--module", "-m", help="app module to run (e.g. :app)")
    p.add_argument("--serial", "-s", help="device serial from `adb devices` (default: $ANDROID_SERIAL, else the only device that is connected)")
    p.add_argument("--adb", metavar="PATH", help="the adb executable, or the folder that holds it")
    v4_flags(p)
    optimize_flags(p)
    react_flags(p)
    p.add_argument("--no-incremental", action="store_true",
                   help="pass --no-incremental to adb install: send the whole APK before it is installed, even when a .idsig lies beside it (adb 30+; without it adb streams "
                        "incrementally when it can)")
    p.add_argument("--reinstall", action="store_true",
                   help="when the device refuses the update (a different signing key, or a lower versionCode) uninstall the app first: this DELETES the app's data on the device")
    p.add_argument("--activity", help="start this activity instead of the launcher activity (.Name, Name, or a full class name)")
    p.add_argument("--logcat", action="store_true", help="after the start, follow logcat for the app's process until Ctrl-C")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("publish", help="publish a library module into a Maven-layout directory")
    common(p)
    p.add_argument("--module", "-m", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--group")
    p.add_argument("--version-name", dest="version")
    p.add_argument("--release", action="store_true")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("vendor", help="copy every dependency file into vendor/ (Maven layout) so builds can run offline")
    common(p)
    p.add_argument("--module", "-m", help="only this module and what it needs (default: all modules)")
    p.set_defaults(fn=cmd_vendor)

    p = sub.add_parser("clean", help="delete .apkforge/ and build/")
    common(p)
    p.set_defaults(fn=cmd_clean)

    p = sub.add_parser("new", help="create a project that builds as-is")
    p.add_argument("dir")
    p.add_argument("--template", "-t", default="java", choices=["java", "kotlin", "multi"])
    p.add_argument("--name")
    p.add_argument("--package", help="application id and code package, e.g. com.acme.notes (default: com.example.<name>, which Google Play rejects)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("keygen", help="create a release keystore and the matching \"signing\" section in apkforge.json",
                       epilog="The store password is read from $APKFORGE_STORE_PASS when that is set, otherwise generated and printed once. It is never written to the project: "
                              "apkforge.json only names the variable (storePasswordEnv). An existing keystore is never overwritten.")
    common(p)
    p.add_argument("--keystore", help="keystore file to create (default: <project>/release.p12, upload.p12 with --upload)")
    p.add_argument("--alias", help="key alias (default: release, upload with --upload)")
    p.add_argument("--dname", help='certificate subject, e.g. "CN=Acme, O=Acme GmbH, C=DE" (default: CN=<applicationId>)')
    p.add_argument("--password-env", default=None, help="environment variable that holds the store password (default: APKFORGE_STORE_PASS, APKFORGE_UPLOAD_PASS with --upload)")
    p.add_argument("--upload", action="store_true", help='create the upload key for the Android App Bundle and the "uploadSigning" section (optional: without it the bundle is signed with "signing")')
    p.add_argument("--force", action="store_true", help='replace an existing "signing" (or "uploadSigning") section')
    p.set_defaults(fn=cmd_keygen)

    p = sub.add_parser("doctor", help="provision and check the toolchain")
    common(p, path=False)
    p.add_argument("--kotlin", action="store_true", help="also provision the Kotlin compiler")
    p.add_argument("--ksp", action="store_true", help="also provision the KSP2 runner (for the \"ksp\" dependency scope)")
    p.add_argument("--bundletool", action="store_true")
    p.add_argument("--native", action="store_true", help="also provision what \"native\" (CMake) builds need: CMake, Ninja and an Android NDK (the NDK is a download of several hundred MB from Google)")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("verify", help="verify an APK (signatures, alignment) and say which claims an external tool confirmed",
                       epilog="Each claim is tagged: [internal only] = checked by apkforge's own code; [apksigner], [zipalign], [readelf], [internal + apksigner] = an external tool agreed; "
                              "[not checked; why] = nobody checked it. A <apk>.idsig beside the APK is checked too (v4). Exit status 1 when anything rejects the APK. An .aab is read as an Android App Bundle: its JAR signature, proto manifest and BundleConfig.pb are checked by apkforge, and by jarsigner and bundletool when they are installed.")
    p.add_argument("apk", help="the APK, or the Android App Bundle (.aab)")
    p.add_argument("--elf", choices=("error", "warn", "off"), default="error",
                   help="64-bit native libraries that are not 16 KB page-size ready (Google Play, targetSdk 35+): error = exit status 1 (default), warn = say so, off = skip the check")
    p.add_argument("--play", action="store_true", help="App Bundles: what Google Play will object to (debug certificate, android:debuggable, no signature) counts as a failure")
    p.add_argument("--allow-unsigned", action="store_true", help="App Bundles: a bundle without a signature is not a failure (it is signed somewhere else)")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("test-bundle", help="try an Android App Bundle out here: bundletool's universal APK of it, signed and checked, optionally installed and started",
                       epilog="PATH is a project (it is built with --aab first) or a .aab file that exists.  The APK is the one bundletool makes with --mode=universal, laid out and signed the way "
                              "apkforge signs APKs: with the module's \"signing\" key, else the debug key (not the upload key).  Its package and launcher activity are checked against the bundle's "
                              "(aapt2 reads the APK).  With --install it is installed over adb and the launcher activity is started, as `apkforge run` does.")
    common(p)
    p.add_argument("--release", action="store_true")
    p.add_argument("--module", "-m", help="app module to build (e.g. :app)")
    optimize_flags(p)
    react_flags(p)
    p.add_argument("--install", action="store_true", help="install the APK on a device or emulator over adb and start it")
    p.add_argument("--serial", "-s", help="device serial from `adb devices` (default: $ANDROID_SERIAL, else the only device that is connected)")
    p.add_argument("--adb", metavar="PATH", help="the adb executable, or the folder that holds it")
    p.add_argument("--reinstall", action="store_true", help="when the device refuses the update (a different signing key, or a lower versionCode) uninstall the app first: this DELETES the app's data on the device")
    p.add_argument("--logcat", action="store_true", help="with --install: after the start, follow logcat for the app's process until Ctrl-C")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(fn=cmd_test_bundle)

    p = sub.add_parser("import-gradle", help="write apkforge.json from an existing Gradle project (no Gradle needed)")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--dry-run", action="store_true", help="print the result, write nothing")
    p.add_argument("--force", action="store_true", help="overwrite an existing apkforge.json")
    p.add_argument("-v", "--verbose", action="store_true", help="also list every statement that was ignored on purpose (test dependencies, rootProject.name, ...)")
    p.set_defaults(fn=cmd_import_gradle)

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    try:
        return a.fn(a) or 0
    except ForgeError as e:
        print("\nERROR: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
