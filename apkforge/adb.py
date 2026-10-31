"""adb for `apkforge run`: find it, pick one device, install, start the launcher activity, follow the log.  Standard library only.

Nothing here builds anything and nothing is cached: every call asks the device.  Every adb call has a time limit, so a phone that fell asleep ends as
one ForgeError instead of a hang.  After a device is chosen every call carries `-s SERIAL`: adb itself refuses to guess when two are connected,
and apkforge never relies on ANDROID_SERIAL being read by adb (it reads the variable itself, once, in pick_device()).

The parsers (parse_devices, parse_start) and the choices (find_adb, pick_device) take their input as arguments, so tests run them without a device;
tests/test_aab.py stands in for the executable with a small shell script when a whole run is needed.  What no test here can show is a real phone.
"""
import os
import re
import shlex
import subprocess
import sys
import time

from . import util
from .util import ForgeError, detail, warn

MAIN_ACTION = "android.intent.action.MAIN"
CATEGORY = "android.intent.category."
PLATFORM_TOOLS = "https://developer.android.com/tools/releases/platform-tools"


def _indent(text, by="    "):
    lines = [l for l in text.splitlines() if l.strip()][:12]
    return "\n".join(by + l for l in lines) or by + "(no output)"


# ------------------------------------------------------------------------------------------------------------- finding adb
def _sdk_dirs(env, home, host):
    dirs = [("$" + k, env[k]) for k in ("ANDROID_HOME", "ANDROID_SDK_ROOT") if env.get(k)]
    if host == "windows":
        if env.get("LOCALAPPDATA"):
            dirs.append(("the default SDK folder", os.path.join(env["LOCALAPPDATA"], "Android", "Sdk")))
    elif host == "macos":
        dirs.append(("the default SDK folder", os.path.join(home, "Library", "Android", "sdk")))
    else:
        dirs.append(("the default SDK folder", os.path.join(home, "Android", "Sdk")))
    return dirs


def find_adb(explicit=None, env=None, which=None, home=None, host=None):
    """-> (path to adb, where it was found).  Order: --adb (a file, or a folder holding adb), PATH, $ANDROID_HOME, $ANDROID_SDK_ROOT, the folder Android
    Studio installs the SDK to.  Raises ForgeError that says everything that was tried."""
    env = os.environ if env is None else env
    which = which or util.which
    home = home or os.path.expanduser("~")
    host = host or util.host_os()
    name = "adb.exe" if host == "windows" else "adb"

    def usable(p):
        return os.path.isfile(p) and os.access(p, os.X_OK)

    if explicit:
        p = explicit if os.path.isfile(explicit) else os.path.join(explicit, name)
        if not usable(p):
            raise ForgeError("--adb %s: that is not an executable file, nor a folder with %s in it" % (explicit, name))
        return os.path.abspath(p), "--adb"
    p = which("adb")
    if p:
        return p, "PATH"
    looked = ["PATH (no adb)"]
    sdk = _sdk_dirs(env, home, host)
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if not env.get(var):
            looked.append("$%s (not set)" % var)
    for label, d in sdk:
        p = os.path.join(d, "platform-tools", name)
        if usable(p):
            return p, label if label.startswith("$") else "%s: %s" % (label, d)
        looked.append("%s (%s: no adb there)" % (p, label))
    raise ForgeError("adb was not found.\n  looked in: %s\n  Install the Android SDK Platform-Tools (%s; Debian/Ubuntu: apt install adb), then put adb on the PATH, "
                     "set ANDROID_HOME, or pass --adb /path/to/adb" % ("; ".join(looked), PLATFORM_TOOLS))


# ------------------------------------------------------------------------------------------------------------- one adb call
class Adb:
    """One adb executable and, once a device is chosen, that device (-s SERIAL on every call)."""

    def __init__(self, path, serial=None):
        self.path, self.serial = path, serial

    def argv(self, *args):
        return [self.path] + (["-s", self.serial] if self.serial else []) + [str(a) for a in args]

    def call(self, *args, timeout=60):
        """-> (returncode, stdout, stderr).  Raises ForgeError when adb cannot be run or does not answer in `timeout` seconds."""
        cmd = self.argv(*args)
        detail("$ " + " ".join(shlex.quote(c) for c in cmd))
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ForgeError("adb %s did not answer within %d s (a device that is asleep, offline or stuck? `adb kill-server` and try again)" % (args[0] if args else "", timeout))
        except OSError as e:
            raise ForgeError("cannot run adb (%s): %s" % (self.path, e))
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        for text in (out, err):
            if text.strip():
                detail(text.rstrip())
        return p.returncode, out, err

    def shell(self, *words, timeout=60):
        """`adb shell` joins its arguments and gives them to the device's shell: quote each word, or `Outer$Inner` becomes a variable"""
        return self.call("shell", " ".join(shlex.quote(w) for w in words), timeout=timeout)


# ------------------------------------------------------------------------------------------------------------- devices
_DEVICE = re.compile(r"^(\S+)\s+(no permissions|\S+)(?:\s+(.*))?$")

STATE_HELP = {
    "unauthorized": "it has not accepted this computer's debugging key: unlock it and tap Allow on the \"Allow USB debugging?\" prompt (if none appears, unplug it, `adb kill-server`, plug it in again)",
    "authorizing": "it is still waiting for the \"Allow USB debugging?\" prompt: accept it on the device",
    "offline": "it is connected but not answering: unplug and re-plug it, or `adb kill-server`, then try again",
    "connecting": "adb is still connecting to it: wait a moment and try again",
    "no permissions": "adb may not use the USB device: check the udev rules and the plugdev group (or `adb kill-server`, then start adb again as the right user)",
    "bootloader": "it is in the bootloader, not booted into Android",
    "recovery": "it is in recovery, not booted into Android",
    "sideload": "it is in sideload mode, not booted into Android",
}


def parse_devices(text):
    """The stdout of `adb devices -l` -> [{"serial", "state", "model"}].  Daemon notices (`* daemon started successfully`) and the header are skipped."""
    out, listed = [], False
    for line in text.splitlines():
        if line.startswith("List of devices attached"):
            listed = True
            continue
        if not listed or not line.strip() or line.startswith(("*", "adb server")):
            continue
        m = _DEVICE.match(line.strip())
        if m:
            model = re.search(r"\bmodel:(\S+)", m.group(3) or "")
            out.append({"serial": m.group(1), "state": m.group(2), "model": model.group(1) if model else None})
    return out


def devices(adb_path):
    rc, out, err = Adb(adb_path).call("devices", "-l", timeout=60)          # the first call may start the adb server
    if "List of devices attached" not in out:
        raise ForgeError("`adb devices` failed (exit %d):\n%s" % (rc, _indent(err or out)))
    return parse_devices(out)


def device_label(d):
    return "%s (%s)" % (d["serial"], d["model"]) if d["model"] else d["serial"]


def pick_device(found, wanted=None, env_serial=None):
    """The one device to use: -s SERIAL, else $ANDROID_SERIAL, else the only device that is ready.  -> its dict.  Raises ForgeError that says what to do."""
    serial = wanted or env_serial
    if serial:
        src = "-s" if wanted else "ANDROID_SERIAL"
        d = next((x for x in found if x["serial"] == serial), None)
        if d is None:
            raise ForgeError("%s %s: no such device.  Connected: %s" % (src, serial, ", ".join(device_label(x) + " " + x["state"] for x in found) or "none (see `adb devices`)"))
        if d["state"] != "device":
            raise ForgeError("device %s is %s: %s" % (serial, d["state"], STATE_HELP.get(d["state"], "adb does not list it as ready (`adb devices`)")))
        return d
    ready = [d for d in found if d["state"] == "device"]
    if len(ready) == 1:
        return ready[0]
    if len(ready) > 1:
        raise ForgeError("%d devices are connected: %s\n  choose one with -s SERIAL (or set ANDROID_SERIAL)" % (len(ready), ", ".join(device_label(d) for d in ready)))
    if found:
        raise ForgeError("no device is ready:\n" + "\n".join("    %s is %s: %s" % (d["serial"], d["state"], STATE_HELP.get(d["state"], "adb does not list it as ready")) for d in found))
    raise ForgeError("no device or emulator is connected.  Plug in a phone with USB debugging turned on (Settings > Developer options), or start an emulator, "
                     "and check that `adb devices` lists it as \"device\"")


# ------------------------------------------------------------------------------------------------------------- install
_FAILURE = re.compile(r"Failure \[([A-Z][A-Z0-9_]*)(?::\s*([^\]]*))?\]")
REINSTALL_CODES = ("INSTALL_FAILED_UPDATE_INCOMPATIBLE", "INSTALL_FAILED_VERSION_DOWNGRADE")
INSTALL_HINTS = {
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "the app on the device was signed with a different key than this APK (a release build over a debug build, or the other way round)",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "the app on the device has a higher versionCode than this APK",
    "INSTALL_FAILED_OLDER_SDK": "the device runs an older Android than the app's minSdk",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "the APK has native libraries but none for this device's CPU (see the jniLibs folders and the \"abis\" setting)",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "the device is out of storage",
    "INSTALL_FAILED_USER_RESTRICTED": "the device blocks installs over USB (on some phones: Settings > Developer options > Install via USB)",
}


def _succeeded(rc, out):
    return rc == 0 and re.search(r"^Success\b", out, re.M) is not None       # an old adb prints Failure [...] on stdout and still exits 0


def _failure(text):
    f = _FAILURE.search(text)
    return (f.group(1), (f.group(2) or "").strip()) if f else (None, "")


def _install_error(code, why, text, tried_reinstall):
    if code is None:
        return "adb install failed:\n" + _indent(text)
    msg = "adb install failed: %s%s" % (code, ": " + why if why else "")
    hint = INSTALL_HINTS.get(code)
    if hint:
        msg += "\n  " + hint
    if code in REINSTALL_CODES and not tried_reinstall:
        msg += "\n  run again with --reinstall to uninstall the app first (this deletes the app's data on the device)"
    return msg


def install(adb, apk, appid, reinstall=False, timeout=600, no_incremental=False):
    """`adb install -r`.  -> "installed" | "reinstalled".  When the device refuses an update (another signing key, or a lower versionCode) the app is only
    uninstalled with reinstall=True: uninstalling deletes its data, so it is never done silently.  Raises ForgeError with the device's own words and a hint.
    no_incremental adds adb's `--no-incremental` (platform-tools 30+): without it adb itself decides, and streams the APK incrementally when a `<apk>.idsig`
    lies beside it and the device supports that."""
    flags = ("-r", "--no-incremental") if no_incremental else ("-r",)
    rc, out, err = adb.call("install", *flags, apk, timeout=timeout)
    if _succeeded(rc, out):
        return "installed"
    text = (out + "\n" + err).strip()
    code, why = _failure(text)
    tried = False
    if reinstall and code in REINSTALL_CODES:
        warn("%s: uninstalling %s first (--reinstall); its data on the device is deleted" % (code, appid))
        rc, uout, uerr = adb.call("uninstall", appid, timeout=120)
        if not _succeeded(rc, uout):
            raise ForgeError("adb uninstall %s failed (exit %d):\n%s" % (appid, rc, _indent((uout + "\n" + uerr).strip())))
        tried = True
        rc, out, err = adb.call("install", *flags, apk, timeout=timeout)
        if _succeeded(rc, out):
            return "reinstalled"
        text = (out + "\n" + err).strip()
        code, why = _failure(text)
    raise ForgeError(_install_error(code, why, text, tried))


# ------------------------------------------------------------------------------------------------------------- start
_JAVA_EXCEPTION = re.compile(r"^(?:java|android)\.[\w.$]*(?:Exception|Error)\b")


def parse_start(text):
    """The output of `am start -W` -> {"status", "state", "total_ms", "wait_ms", "activity", "warning", "error"}.  error = the lines that say it did not start."""
    r = {"status": None, "state": None, "total_ms": None, "wait_ms": None, "activity": None, "warning": None, "error": None}
    errors = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(Status|LaunchState|Activity|TotalTime|WaitTime):\s*(\S+)", line)
        if m:
            key, val = m.groups()
            if key == "Status":
                r["status"] = val.lower()
            elif key == "LaunchState":
                r["state"] = val.upper()
            elif key == "Activity":
                r["activity"] = val
            else:
                r["total_ms" if key == "TotalTime" else "wait_ms"] = int(val) if val.isdigit() else None
        elif line.startswith("Warning:"):
            r["warning"] = line[len("Warning:"):].strip()
        elif line.startswith(("Error", "Exception occurred")) or "Permission Denial" in line or _JAVA_EXCEPTION.match(line):
            errors.append(line)
    r["error"] = "\n".join(errors) or None
    return r


def describe_start(r):
    kind = {"COLD": "cold start", "WARM": "warm start", "HOT": "hot start"}.get(r["state"], "started")
    parts = [kind]
    if r["total_ms"] is not None:
        parts.append("TotalTime %d ms" % r["total_ms"])
    if r["wait_ms"] is not None:
        parts.append("WaitTime %d ms" % r["wait_ms"])
    s = ", ".join(parts)
    if r["warning"]:
        s += "; " + r["warning"]
    return s


def start(adb, appid, component, category="LAUNCHER", timeout=120):
    """`am start -W` with the intent the launcher sends (action MAIN + the category).  -> parse_start() plus "component".  Raises ForgeError when it did not start;
    Status: timeout (the system stopped waiting) is only a warning, so that --logcat can still show why."""
    target = "%s/%s" % (appid, component)
    rc, out, err = adb.shell("am", "start", "-W", "-a", MAIN_ACTION, "-c", CATEGORY + category, "-n", target, timeout=timeout)
    r = parse_start(out + "\n" + err)
    r["component"] = target
    if r["error"] or rc != 0 or r["status"] not in (None, "ok", "timeout"):
        raise ForgeError("could not start %s%s:\n%s" % (target, " (exit %d)" % rc if rc else "", _indent(r["error"] or (out + "\n" + err).strip())))
    if r["status"] == "timeout":
        r["warning"] = "the system stopped waiting for it to finish starting (Status: timeout)"
    return r


# ------------------------------------------------------------------------------------------------------------- the log
def app_pid(adb, appid):
    """The pid of the app's main process, or None when it is not running.  Raises ForgeError on a device without pidof (before Android 6)."""
    rc, out, err = adb.shell("pidof", appid, timeout=30)
    if rc != 0 and "not found" in (out + err):
        raise ForgeError("--logcat needs `pidof` and `logcat --pid` on the device (Android 7.0, API 24, or newer)")
    m = re.search(r"\b(\d+)\b", out) if rc == 0 else None
    return int(m.group(1)) if m else None


def wait_pid(adb, appid, timeout=6.0, interval=0.25, sleep=time.sleep, clock=time.monotonic):
    end = clock() + timeout
    while True:
        pid = app_pid(adb, appid)
        if pid or clock() >= end:
            return pid
        sleep(interval)


def follow_log(adb, appid, out=None, timeout=6.0):
    """logcat for the app's process until Ctrl-C (or until adb ends).  -> exit status: 0 after Ctrl-C or a normal end; 1 when the app has no process
    (it crashed while starting?)."""
    out = out or sys.stdout
    pid = wait_pid(adb, appid, timeout=timeout)
    if not pid:
        warn("no process named %s after %d s: it may have crashed while starting.  The crash log:  adb -s %s logcat -b crash -d" % (appid, timeout, adb.serial or "SERIAL"))
        return 1
    cmd = adb.argv("logcat", "--pid=%d" % pid, "-v", "threadtime")
    detail("$ " + " ".join(shlex.quote(c) for c in cmd))
    print("-- logcat for %s (pid %d); Ctrl-C stops --" % (appid, pid), file=out)
    out.flush()
    interrupted = False
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise ForgeError("cannot run adb (%s): %s" % (adb.path, e))
    with proc:
        try:
            for raw in iter(proc.stdout.readline, b""):
                out.write(raw.decode("utf-8", "replace"))
                out.flush()
        except (KeyboardInterrupt, BrokenPipeError):
            interrupted = True
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
    return 0 if interrupted else proc.returncode
