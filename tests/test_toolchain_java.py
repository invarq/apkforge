"""How apkforge finds Java (the "no Java runtime found" fix of 1.3.0), and the D8 cache that a failed run must not poison.

The reported bug: on a machine with no Java on PATH or JAVA_HOME, `apkforge build` stopped with "no Java runtime found" although `apkforge doctor` had already downloaded
a JDK into <apkforge home>/toolchain/jdk.  Toolchain.java() looked only at JAVA_HOME and PATH.  A second bug sat behind it: _provision_dexer wrote the D8 jar to its cache path
before it had started it, so a run that could not start Java left an unchecked (and, after the fallback source, older) jar there for every later run to use.

Everything here is offline and needs no real JDK: a JDK is a folder of empty files, and nothing is downloaded (ensure_javac is replaced by a tripwire or a stand-in).
Run it from the folder that holds apkforge/ :      python3 -m unittest discover -s tests -v
"""
import contextlib
import glob
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
from apkforge import signing, util                                                  # noqa: E402
from apkforge import toolchain as T                                                 # noqa: E402
from apkforge.util import ForgeError                                                # noqa: E402

REAL_GLOB = glob.glob


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)
    return path


def fake_jdk(toolchain_root, name="jdk-21.0.13+9", macos=False, tools=("java", "javac", "keytool", "jarsigner")):
    """what provision_jdk leaves behind: <toolchain>/jdk/<name>/bin (Linux, Windows) or <name>/Contents/Home/bin (macOS) -> the bin folder"""
    bin_dir = os.path.join(toolchain_root, "jdk", name, *(("Contents", "Home") if macos else ()), "bin")
    for t in tools:
        touch(os.path.join(bin_dir, util.exe(t)))
    return bin_dir


class BareMachine(unittest.TestCase):
    """a machine with no Java: an empty folder as PATH, no JAVA_HOME, an empty apkforge home, and no /usr/lib/jvm"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="apkforge-java-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.empty_bin = os.path.join(self.tmp, "empty-bin")
        os.makedirs(self.empty_bin)
        env = mock.patch.dict(os.environ, {"APKFORGE_HOME": self.home, "PATH": self.empty_bin})
        env.start()
        self.addCleanup(env.stop)
        for var in ("JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT", "APKFORGE_R8_JAR"):
            os.environ.pop(var, None)
        # the JDKs of the machine that runs the tests must not leak into the answers
        no_system_jvm = lambda pattern, *a, **k: [] if pattern.startswith("/usr/lib/jvm") else REAL_GLOB(pattern, *a, **k)    # noqa: E731
        g = mock.patch.object(glob, "glob", no_system_jvm)
        g.start()
        self.addCleanup(g.stop)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)

    def toolchain(self, fetch_missing=True):
        tc = T.Toolchain(fetch_missing=fetch_missing)
        self.assertTrue(tc.root.startswith(self.home), tc.root)
        return tc

    @staticmethod
    def no_download():
        """ensure_javac (what fetches a JDK) replaced by a tripwire: a test that must not download fails loudly when it does"""
        return mock.patch.object(T.Toolchain, "ensure_javac", side_effect=AssertionError("tried to download a JDK"))


class JavaLookupTests(BareMachine):
    def test_a_downloaded_jdk_is_used_when_nothing_else_has_java(self):
        """the reported bug"""
        tc = self.toolchain()
        want = os.path.join(fake_jdk(tc.root), util.exe("java"))
        with self.no_download():
            self.assertEqual(tc.java(), want)

    def test_the_macos_layout_of_the_downloaded_jdk(self):
        tc = self.toolchain()
        want = os.path.join(fake_jdk(tc.root, macos=True), util.exe("java"))
        with self.no_download():
            self.assertEqual(tc.java(), want)

    def test_a_downloaded_jdk_is_found_with_downloads_off(self):
        """--offline, and the commands that never download (verify): a JDK that is already there still counts"""
        tc = self.toolchain(fetch_missing=False)
        want = os.path.join(fake_jdk(tc.root), util.exe("java"))
        self.assertEqual(tc.java(), want)

    def test_order_is_java_home_then_path_then_the_downloaded_jdk(self):
        java = util.exe("java")
        downloaded = os.path.join(fake_jdk(self.toolchain().root), java)
        on_path = touch(os.path.join(self.empty_bin, java))
        java_home = os.path.join(self.tmp, "jh")
        in_home = touch(os.path.join(java_home, "bin", java))
        os.environ["JAVA_HOME"] = java_home
        with self.no_download():
            self.assertEqual(self.toolchain().java(), in_home)          # java() is memoized: a fresh Toolchain for every answer
            os.remove(in_home)
            self.assertEqual(self.toolchain().java(), on_path)
            os.remove(on_path)
            self.assertEqual(self.toolchain().java(), downloaded)

    def test_an_unset_java_home_is_not_the_current_directory(self):
        """os.path.join("", "bin", "java") is bin/java of the current directory"""
        work = os.path.join(self.tmp, "work")
        touch(os.path.join(work, "bin", util.exe("java")))
        os.chdir(work)
        with self.no_download(), self.assertRaises(ForgeError):
            self.toolchain(fetch_missing=False).java()

    def test_nothing_anywhere_and_downloads_off_says_what_to_do(self):
        with self.no_download(), self.assertRaises(ForgeError) as cm:
            self.toolchain(fetch_missing=False).java()
        msg = str(cm.exception)
        for part in ("no Java runtime found", "JAVA_HOME", "--offline", "apkforge doctor"):
            self.assertIn(part, msg)

    def test_nothing_anywhere_and_downloads_on_fetches_a_jdk_once(self):
        tc = self.toolchain()

        def provision():
            return os.path.join(fake_jdk(tc.root), util.exe("javac"))

        with mock.patch.object(T.Toolchain, "ensure_javac", side_effect=provision) as ensure:
            first, second = tc.java(), tc.java()
        self.assertEqual(first, os.path.join(tc.root, "jdk", "jdk-21.0.13+9", "bin", util.exe("java")))
        self.assertEqual(second, first)
        ensure.assert_called_once_with()

    def test_a_failed_jdk_download_is_part_of_the_message(self):
        with mock.patch.object(T.Toolchain, "ensure_javac", side_effect=ForgeError("no javac and cannot install one (network is down)")):
            with self.assertRaises(ForgeError) as cm:
                self.toolchain().java()
        msg = str(cm.exception)
        for part in ("no Java runtime found", "fetching a JDK failed", "network is down"):
            self.assertIn(part, msg)

    def test_a_fetched_jdk_without_java_is_reported(self):
        tc = self.toolchain()
        javac = touch(os.path.join(tc.root, "jdk", "odd", "bin", util.exe("javac")))
        with mock.patch.object(T.Toolchain, "ensure_javac", return_value=javac):
            with self.assertRaises(ForgeError) as cm:
                tc.java()
        self.assertIn("there is no java beside " + javac, str(cm.exception))


class JdkToolTests(BareMachine):
    def test_keytool_and_jarsigner_come_from_the_downloaded_jdk_without_downloading(self):
        tc = self.toolchain()
        bin_dir = fake_jdk(tc.root)
        with self.no_download():
            self.assertEqual(tc.keytool(), os.path.join(bin_dir, util.exe("keytool")))
            self.assertEqual(tc.jarsigner(), os.path.join(bin_dir, util.exe("jarsigner")))

    def test_a_missing_tool_is_none_and_never_a_download(self):
        with self.no_download():
            tc = self.toolchain()
            self.assertIsNone(tc.keytool())
            self.assertIsNone(tc.jarsigner())


@unittest.skipIf(os.name == "nt", "the stand-in keytool is a shell script")
class KeystoreToolTests(BareMachine):
    """signing.keystore_signer converts a JKS keystore with keytool: the one it is given (Toolchain.keytool()), else the one on PATH"""

    def jks(self):
        path = os.path.join(self.tmp, "release.jks")
        with open(path, "wb") as f:
            f.write(b"\xfe\xed\xfe\xed" + b"\0" * 60)
        return path

    def test_the_given_keytool_is_the_one_that_runs(self):
        log = os.path.join(self.tmp, "keytool.args")
        keytool = touch(os.path.join(self.tmp, "jdk", "bin", "keytool"), '#!/bin/sh\necho "$@" > "%s"\nexit 1\n' % log)
        with quiet(), self.assertRaises(ForgeError):
            signing.keystore_signer(self.jks(), "upload", "store-secret", "key-secret", keytool=keytool)
        with open(log) as f:
            args = f.read()
        self.assertIn("-importkeystore", args)
        for secret in ("store-secret", "key-secret"):
            self.assertNotIn(secret, args)                              # the passwords travel in the environment, not in the arguments

    def test_no_keytool_anywhere_points_at_doctor(self):
        with self.assertRaises(ForgeError) as cm:
            signing.keystore_signer(self.jks(), "upload", "pw")
        self.assertIn("keytool is needed", str(cm.exception))
        self.assertIn("apkforge doctor", str(cm.exception))


class DexerCacheTests(BareMachine):
    """dexer() trusts whatever file sits at <toolchain>/dexer/r8-d8.jar, so _provision_dexer may publish a jar there only after it has started"""

    @staticmethod
    def jar(entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in entries.items():
                z.writestr(name, data)
        return buf.getvalue()

    def setUp(self):
        super().setUp()
        self.tc = self.toolchain()
        self.dest = self.tc.path("dexer", "r8-d8.jar")
        dl = self.tc.path("dl")
        os.makedirs(dl)
        r8 = "com/android/tools/r8/D8.class"
        self.sources = {
            "jadx": os.path.join(dl, "jadx-1.5.6.zip"),                 # a zip that holds lib/jadx-1.5.6-all.jar, which holds the R8 classes (the newest D8)
            "bundletool": os.path.join(dl, "bundletool-all-1.18.3.jar"),    # a jar that holds the R8 classes itself (an older D8)
        }
        with zipfile.ZipFile(self.sources["jadx"], "w") as z:
            z.writestr("lib/jadx-1.5.6-all.jar", self.jar({r8: b"JADX-D8", "other/Thing.class": b"x"}))
        with open(self.sources["bundletool"], "wb") as f:
            f.write(self.jar({r8: b"BUNDLETOOL-D8"}))
        self.fetched = []

        def fetch(url, dest, pin_key, label=None, **kw):
            self.fetched.append(pin_key)
            return self.sources[pin_key]

        for patch in (mock.patch.object(T, "fetch", side_effect=fetch), mock.patch.object(T.Toolchain, "sdk", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def starts(*markers):
        """a stand-in for run(): `java -cp <jar> ... D8 --version` answers only for jars whose D8.class holds one of the markers"""
        def run(cmd, check=True, **kw):
            with zipfile.ZipFile(cmd[cmd.index("-cp") + 1]) as z:
                body = z.read("com/android/tools/r8/D8.class")
            return (0, "D8 9.1.31 (fake)") if body in markers else (1, "Error: could not start")
        return run

    def provision(self):
        with quiet():
            return self.tc.dexer()

    def test_the_first_source_is_published_once_it_has_started(self):
        with mock.patch.object(T.Toolchain, "java", return_value="java"), mock.patch.object(T, "run", side_effect=self.starts(b"JADX-D8", b"BUNDLETOOL-D8")):
            self.assertEqual(self.provision(), self.dest)
        with zipfile.ZipFile(self.dest) as z:
            self.assertEqual(z.read("com/android/tools/r8/D8.class"), b"JADX-D8")
            self.assertNotIn("other/Thing.class", z.namelist())         # only the R8 classes are carved out of the fat jar
        self.assertEqual(self.fetched, ["jadx"])                        # the older bundletool D8 is never fetched, let alone allowed to overwrite it
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["r8-d8.jar"])

    def test_when_java_cannot_start_nothing_is_left_behind(self):
        """the bug: the jar was already at its cache path when java() raised, and the second source then overwrote it with the older D8"""
        with mock.patch.object(T.Toolchain, "java", side_effect=ForgeError("no Java runtime found")):
            with self.assertRaises(ForgeError) as cm:
                self.provision()
        self.assertIn("cannot provision D8", str(cm.exception))
        self.assertIn("no Java runtime found", str(cm.exception))
        self.assertEqual(self.fetched, ["jadx", "bundletool"])
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), [])    # no r8-d8.jar, no candidate, no .part

    def test_a_jar_that_does_not_start_is_not_published_and_the_next_source_is_tried(self):
        with mock.patch.object(T.Toolchain, "java", return_value="java"), mock.patch.object(T, "run", side_effect=self.starts(b"BUNDLETOOL-D8")):
            self.assertEqual(self.provision(), self.dest)
        with zipfile.ZipFile(self.dest) as z:
            self.assertEqual(z.read("com/android/tools/r8/D8.class"), b"BUNDLETOOL-D8")
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["r8-d8.jar"])

    def test_a_failed_run_does_not_decide_the_next_one(self):
        """fail on a bare machine, fix the machine, run again: the second run provisions the newest D8 instead of trusting a leftover"""
        with mock.patch.object(T.Toolchain, "java", side_effect=ForgeError("no Java runtime found")):
            with self.assertRaises(ForgeError):
                self.provision()
        self.tc = self.toolchain()                                      # a new process: nothing is memoized
        with mock.patch.object(T.Toolchain, "java", return_value="java"), mock.patch.object(T, "run", side_effect=self.starts(b"JADX-D8", b"BUNDLETOOL-D8")):
            self.assertEqual(self.provision(), self.dest)
        with zipfile.ZipFile(self.dest) as z:
            self.assertEqual(z.read("com/android/tools/r8/D8.class"), b"JADX-D8")


if __name__ == "__main__":
    unittest.main()
