"""Tests for google-services.json and Crashlytics: gms.py (the file, its resources, the mapping id, the upload command) and what build.py, project.py, cli.py and gradle_import.py do with it.

Run it from the folder that holds apkforge/ :      python3 -m unittest discover -s tests -v
The demo builds at the end need aapt2, D8, android.jar, bundletool and a JDK, and skip themselves when those are not installed.  Nothing here uses the network: the Firebase dependencies of the
auto-enable test are two stand-in jars (one class each, built with the toolchain's javac) in a local Maven repository, and the Firebase CLI is a bash script.

What the parser and the search order are held to is Google's source (github.com/google/play-services-plugins, google-services-plugin: GoogleServicesTask.kt and GoogleServicesPlugin.kt),
not the code under test: `google_locations` below is that source's getJsonLocations, transcribed, and the expected values are those its handle* functions produce.
"""
import contextlib
import copy
import io
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
import xml.etree.ElementTree as ET
import zipfile
from unittest import mock

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)
from apkforge import cli, deps, gms, gradle_import, project, templates, util        # noqa: E402
from apkforge import build as B                                                    # noqa: E402
from apkforge import toolchain as T                                                # noqa: E402
from apkforge.util import ForgeError                                               # noqa: E402

PKG = "com.acme.gms"
APP_ID = "1:1035469437089:android:73a4fb8297b2cd4f"
KEY = "AIzaSyDEMO-key_with'quote&amp"                      # an apostrophe and an ampersand: what xml_text is for
MID = "0123456789abcdef0123456789abcdef"


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def run_cli(*argv, env=None):
    """one `apkforge` command in this process -> (exit status, stdout, stderr).  util._CONSOLE is reset for the call, as in the other test files"""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}), mock.patch.object(util, "_CONSOLE", None), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def installed_toolchain():
    """the toolchain apkforge already has on this machine, or None"""
    try:
        tc = T.Toolchain(fetch_missing=False)
        return tc if (tc.aapt2() and tc.dexer() and tc.bundletool() and tc.java() and tc.javac()) else None
    except (ForgeError, OSError):
        return None


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text if isinstance(text, str) else json.dumps(text))
    if mode:
        os.chmod(path, mode)
    return path


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def gs_data(package=PKG, key=KEY):
    """a realistic google-services.json, as a dict: an app of another package first, then the one for `package`"""
    other = {"client_info": {"mobilesdk_app_id": "1:1035469437089:android:00000000other", "android_client_info": {"package_name": "com.acme.other"}},
             "api_key": [{"current_key": "OTHER-KEY"}], "services": {}}
    mine = {"client_info": {"mobilesdk_app_id": APP_ID, "android_client_info": {"package_name": package}},
            "oauth_client": [{"client_id": "android.apps.googleusercontent.com", "client_type": 1},
                             {"client_id": "web-one.apps.googleusercontent.com", "client_type": 3},
                             {"client_id": "web-two.apps.googleusercontent.com", "client_type": 3}],
            "api_key": [{"current_key": key}],
            "services": {"analytics_service": {"status": 2, "analytics_property": {"tracking_id": "UA-65557217-3"}}, "maps_service": {"status": 2}}}
    return {"project_info": {"project_number": "1035469437089", "firebase_url": "https://mydemoapp.firebaseio.com", "project_id": "mydemoapp", "storage_bucket": "mydemoapp.appspot.com"},
            "client": [other, mine]}


def mine(data):
    return data["client"][1]


EXPECTED = {            # what GoogleServicesTask's handle* functions make of gs_data(), in the order of its TreeMap
    "default_web_client_id": "web-one.apps.googleusercontent.com", "firebase_database_url": "https://mydemoapp.firebaseio.com", "ga_trackingId": "UA-65557217-3",
    "gcm_defaultSenderId": "1035469437089", "google_api_key": KEY, "google_app_id": APP_ID, "google_crash_reporting_api_key": KEY, "google_maps_key": KEY,
    "google_storage_bucket": "mydemoapp.appspot.com", "project_id": "mydemoapp"}


def unescape(text):
    """what aapt2 makes of the text of a <string>: a backslash escapes the character after it"""
    return re.sub(r"\\(.)", r"\1", text)


def strings_of(xml_text):
    """{name: the value aapt2 would read} of a <resources> document"""
    root = ET.fromstring(xml_text.encode("utf-8"))
    return {e.get("name"): unescape(e.text or "") for e in root.findall("string")}


# ======================================================================================================================= the parser
class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="apkforge-gms-parse-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def file(self, data):
        return write(os.path.join(self.tmp, "google-services.json"), data)

    def parse(self, data, package=PKG):
        return gms.parse(self.file(data), package)

    def refuses(self, data, *fragments, package=PKG):
        path = self.file(data)
        with self.assertRaises(ForgeError) as cm:
            gms.parse(path, package)
        text = str(cm.exception)
        self.assertIn(path, text)                                          # the file is always named
        for f in fragments:
            self.assertIn(f, text)
        return text

    def test_a_realistic_file(self):
        g = self.parse(gs_data())
        self.assertEqual(g.values, EXPECTED)
        self.assertEqual(list(g.values), sorted(EXPECTED))                 # the plugin keeps its resources in a TreeMap
        self.assertEqual((g.app_id, g.tracker, g.application_id, g.warnings), (APP_ID, "UA-65557217-3", PKG, []))
        self.assertEqual(g.path, os.path.join(self.tmp, "google-services.json"))

    def test_the_client_is_the_one_for_the_applicationid(self):
        d = gs_data()
        self.assertEqual(self.parse(d, "com.acme.other").values["google_api_key"], "OTHER-KEY")
        self.assertEqual(self.parse(d, PKG).values["google_api_key"], KEY)
        d["client"].insert(0, {"client_info": {"mobilesdk_app_id": "x"}})                          # no android_client_info: not an Android app, skipped
        d["client"].insert(0, "not an object")
        self.assertEqual(self.parse(d, PKG).values["google_app_id"], APP_ID)

    def test_the_first_client_of_a_package_wins(self):
        d = gs_data()
        twin = copy.deepcopy(mine(d))
        twin["api_key"] = [{"current_key": "SECOND"}]
        d["client"].append(twin)
        self.assertEqual(self.parse(d).values["google_api_key"], KEY)

    def test_no_client_for_the_applicationid_lists_the_clients_of_the_file(self):
        text = self.refuses(gs_data(), "com.acme.nope", "com.acme.gms", "com.acme.other", package="com.acme.nope")
        self.assertIn("applicationId", text)
        d = gs_data()
        d["client"] = []
        self.refuses(d, "(its clients: none)")

    def test_the_maps_key_only_at_status_2(self):
        for status, expect in ((2, True), ("2", True), (1, False), ("1", False)):
            d = gs_data()
            mine(d)["services"]["maps_service"]["status"] = status
            self.assertEqual("google_maps_key" in self.parse(d).values, expect, status)
        d = gs_data()
        del mine(d)["services"]["maps_service"]
        g = self.parse(d)
        self.assertNotIn("google_maps_key", g.values)
        self.assertEqual(g.warnings, [])                                  # a service that is off is not worth a word

    def test_an_unknown_status_is_a_warning_and_the_service_is_left_out(self):
        d = gs_data()
        mine(d)["services"]["analytics_service"]["status"] = 3
        mine(d)["services"]["maps_service"]["status"] = "7"
        g = self.parse(d)
        self.assertNotIn("ga_trackingId", g.values)
        self.assertNotIn("google_maps_key", g.values)
        self.assertIsNone(g.tracker)
        self.assertEqual(len(g.warnings), 2)
        self.assertIn("analytics_service", g.warnings[0] + g.warnings[1])
        self.assertTrue(any("status 3" in w for w in g.warnings) and any("status 7" in w for w in g.warnings), g.warnings)

    def test_the_hyphenated_analytics_service_of_googles_page_is_not_read(self):
        d = gs_data()
        svc = mine(d)["services"]
        svc["analytics-service"] = svc.pop("analytics_service")
        g = self.parse(d)
        self.assertNotIn("ga_trackingId", g.values)
        self.assertIsNone(g.tracker)

    def test_an_enabled_analytics_service_without_a_property_is_quiet(self):
        d = gs_data()
        del mine(d)["services"]["analytics_service"]["analytics_property"]
        g = self.parse(d)
        self.assertEqual((g.tracker, g.warnings), (None, []))
        self.assertNotIn("ga_trackingId", g.values)

    def test_what_the_plugin_refuses(self):
        def cut(fn):
            d = gs_data()
            fn(d)
            return d
        self.refuses("{not json", "not valid JSON")
        self.refuses("[]", "malformed root json")
        self.refuses({"client": []}, "missing project_info object")
        self.refuses(cut(lambda d: d.update(project_info=[])), "missing project_info object")
        self.refuses(cut(lambda d: d["project_info"].pop("project_number")), "project_info/project_number")
        self.refuses(cut(lambda d: d["project_info"].pop("project_id")), "project_info/project_id")
        self.refuses(cut(lambda d: mine(d).pop("api_key")), "no api_key/current_key")
        self.refuses(cut(lambda d: mine(d).update(api_key=[{"other": "x"}, "text"])), "no api_key/current_key")
        self.refuses(cut(lambda d: (mine(d).pop("api_key"), mine(d)["services"].pop("maps_service"))), "no api_key/current_key")        # the plugin needs a key with maps off too
        self.refuses(cut(lambda d: mine(d)["client_info"].pop("mobilesdk_app_id")), "mobilesdk_app_id")
        self.refuses(cut(lambda d: mine(d)["client_info"].update(mobilesdk_app_id="")), "mobilesdk_app_id")

    def test_optional_project_info_keys(self):
        d = gs_data()
        del d["project_info"]["storage_bucket"], d["project_info"]["firebase_url"]
        g = self.parse(d)
        self.assertNotIn("google_storage_bucket", g.values)
        self.assertNotIn("firebase_database_url", g.values)

    def test_numbers_and_booleans_become_text(self):
        d = gs_data()
        d["project_info"]["project_number"] = 1035469437089
        d["project_info"]["storage_bucket"] = True
        mine(d)["services"]["analytics_service"]["analytics_property"]["tracking_id"] = 12345
        mine(d)["oauth_client"][1]["client_type"] = "3"                   # as a string too
        g = self.parse(d)
        self.assertEqual((g.values["gcm_defaultSenderId"], g.values["google_storage_bucket"], g.values["ga_trackingId"]), ("1035469437089", "true", "12345"))
        self.assertEqual(g.values["default_web_client_id"], "web-one.apps.googleusercontent.com")

    def test_the_first_web_client_with_an_id_wins(self):
        d = gs_data()
        mine(d)["oauth_client"] = [{"client_type": 3}, {"client_id": "ios", "client_type": 2}, "x", {"client_id": "first-usable", "client_type": 3}, {"client_id": "later", "client_type": 3}]
        self.assertEqual(self.parse(d).values["default_web_client_id"], "first-usable")
        mine(d)["oauth_client"] = [{"client_id": "android", "client_type": 1}]
        self.assertNotIn("default_web_client_id", self.parse(d).values)

    def test_files_are_what_the_build_writes(self):
        g = self.parse(gs_data())
        files = g.files()
        self.assertEqual(sorted(files), sorted([gms.TRACKER_FILE, gms.VALUES_FILE]))
        self.assertEqual(strings_of(files[gms.VALUES_FILE]), EXPECTED)
        self.assertEqual(strings_of(files[gms.TRACKER_FILE]), {"ga_trackingId": "UA-65557217-3"})
        skipped = g.files(skip={"google_app_id", "project_id"})
        self.assertEqual(strings_of(skipped[gms.VALUES_FILE]), {k: v for k, v in EXPECTED.items() if k not in ("google_app_id", "project_id")})
        d = gs_data()
        del mine(d)["services"]["analytics_service"]
        self.assertEqual(list(self.parse(d).files()), [gms.VALUES_FILE])           # no tracking id: no xml/global_tracker.xml


class XmlTextTests(unittest.TestCase):
    def test_escapes(self):
        for raw, escaped in (("it's", "it\\'s"), ('say "hi"', 'say \\"hi\\"'), ("a&b", "a&amp;b"), ("<x>", "&lt;x&gt;"), ("a\\b", "a\\\\b"),
                             ("@string/x", "\\@string/x"), ("?attr/x", "\\?attr/x"), ("a@b", "a@b"), ("a?b", "a?b")):
            self.assertEqual(gms.xml_text(raw), escaped, raw)

    def test_an_empty_value_stays_empty(self):
        """`v[:1] in "@?"` is True for "" in Python, so an empty value was written as a lone backslash"""
        self.assertEqual(gms.xml_text(""), "")
        self.assertEqual(gms.xml_text("a@b"), "a@b")
        self.assertNotIn("\\", gms.render_strings({"k": ""}))

    def test_what_aapt2_reads_is_what_was_written(self):
        values = {"quote": "it's", "dq": 'a "b"', "amp": "a&b&amp;c", "lt": "<a href='x'>", "bs": "C:\\new\\table", "at": "@id/x", "qm": "?x", "plain": "AIzaSyDEMO-key_1",
                  "uni": "caf\u00e9 \u2603 \u00fc", "empty": "", "mixed": "\\'\"&<>@"}
        text = gms.render_strings(values)
        self.assertEqual(strings_of(text), values)                                        # parses as XML, and unescaping gives every value back
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'))
        self.assertTrue(text.endswith("</resources>\n"))
        self.assertEqual(text.count('translatable="false"'), len(values))


# ================================================================================================================ where the file is
def google_locations(build_type, flavor_names):
    """GoogleServicesPlugin.getJsonLocations, transcribed from Google's source (play-services-plugins, google-services-plugin/src/main/kotlin/com/google/gms/googleservices/
    GoogleServicesPlugin.kt), then what Gradle's File.resolve does to the path: normalised, and a place that normalises to an earlier one dropped (the same file)"""
    cap = lambda s: s[:1].upper() + s[1:]                                              # noqa: E731
    flavor_name = ""
    for b in flavor_names:
        flavor_name = flavor_name + (b if not flavor_name else cap(b))
    locs = ["", "src/%s/%s" % (flavor_name, build_type), "src/%s/%s" % (build_type, flavor_name), "src/%s" % flavor_name, "src/%s" % build_type,
            "src/" + flavor_name + cap(build_type), "src/%s" % build_type]
    loc = "src"
    for flavor in flavor_names:
        loc += "/%s" % flavor
        locs += [loc, "%s/%s" % (loc, build_type), loc + cap(build_type)]
    ordered = sorted(dict.fromkeys(locs), key=lambda p: -p.count("/"))               # distinct(), then sortedByDescending: stable
    names = [("google-services.json" if not p else "%s/google-services.json" % p) for p in ordered]
    return list(dict.fromkeys(posixpath.normpath(n) for n in names))


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="apkforge-gms-loc-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_golden_lists(self):
        self.assertEqual(gms.json_locations("debug"), ["src/debug/google-services.json", "src/google-services.json", "src/Debug/google-services.json", "google-services.json"])
        self.assertEqual(gms.json_locations("debug", ["free"]), ["src/free/debug/google-services.json", "src/debug/free/google-services.json", "src/free/google-services.json",
                                                                 "src/debug/google-services.json", "src/freeDebug/google-services.json", "google-services.json"])
        self.assertEqual(gms.json_locations("release", ["free", "arm"]), [      # two dimensions, worked out by hand from getJsonLocations: deepest first, ties in the order they were added
            "src/free/arm/release/google-services.json", "src/freeArm/release/google-services.json", "src/release/freeArm/google-services.json", "src/free/release/google-services.json",
            "src/free/arm/google-services.json", "src/free/armRelease/google-services.json", "src/freeArm/google-services.json", "src/release/google-services.json",
            "src/freeArmRelease/google-services.json", "src/free/google-services.json", "src/freeRelease/google-services.json", "google-services.json"])

    def test_the_search_order_is_googles(self):
        for bt in ("debug", "release", "staging", "benchmarkRelease"):
            for flavors in ((), ("free",), ("free", "arm"), ("demo", "x86", "play"), ("Paid",)):
                self.assertEqual(gms.json_locations(bt, flavors), google_locations(bt, flavors), (bt, flavors))

    def put(self, rel):
        return write(os.path.join(self.tmp, *rel.split("/")), "{}")

    def test_the_first_file_that_exists_wins(self):
        root = self.put("google-services.json")
        self.assertEqual(gms.find_json(self.tmp, "debug")[0], root)
        src = self.put("src/google-services.json")
        self.assertEqual(gms.find_json(self.tmp, "debug")[0], src)                        # src/ comes before the module directory
        dbg = self.put("src/debug/google-services.json")
        self.assertEqual(gms.find_json(self.tmp, "debug")[0], dbg)
        self.assertEqual(gms.find_json(self.tmp, "release")[0], src)                      # a release build does not look in src/debug
        rel = self.put("src/release/google-services.json")
        self.assertEqual(gms.find_json(self.tmp, "release")[0], rel)

    def test_flavor_places(self):
        self.put("google-services.json")
        free = self.put("src/free/google-services.json")
        free_debug = self.put("src/freeDebug/google-services.json")
        # at equal depth the order is the one getJsonLocations adds them in, and it adds src/<flavor> before src/<flavor><BuildType>: so the file of the flavor alone wins
        self.assertEqual(gms.find_json(self.tmp, "debug", ["free"])[0], free)
        os.remove(free)
        self.assertEqual(gms.find_json(self.tmp, "debug", ["free"])[0], free_debug)       # only without it does src/freeDebug count
        deep = self.put("src/free/debug/google-services.json")
        self.assertEqual(gms.find_json(self.tmp, "debug", ["free"])[0], deep)             # a deeper place beats both
        self.assertEqual(gms.find_json(self.tmp, "release", ["free"])[0], os.path.join(self.tmp, "google-services.json"))      # nothing for release: the module directory

    def test_nothing_found_returns_what_was_looked_at(self):
        path, cands = gms.find_json(self.tmp, "debug")
        self.assertIsNone(path)
        self.assertEqual([os.path.relpath(c, self.tmp).replace(os.sep, "/") for c in cands], gms.json_locations("debug"))
        self.assertTrue(all(os.path.isabs(c) for c in cands))


# ================================================================================================================ the decisions
class DecisionTests(unittest.TestCase):
    GMS = ["com.google.firebase:firebase-analytics", "com.google.android.gms:play-services-maps"]
    OTHER = ["com.squareup.okhttp3:okhttp", "com.google.firebasex:firebase-x", "com.google.android.gms.extra:thing", "androidx.core:core"]

    def test_wanted_truth_table(self):
        for setting in (None, "auto"):
            for names in ([], self.OTHER, self.GMS[:1], self.GMS[1:], self.OTHER + self.GMS):
                for have in (True, False):
                    expect = have and any(n in self.GMS for n in names)
                    self.assertEqual(gms.wanted(setting, have, names), expect, (setting, names, have))
        for names in ([], self.OTHER, self.GMS):
            for have in (True, False):
                self.assertTrue(gms.wanted(True, have, names))                          # true processes it anyway (and the build says so if the file is missing)
                self.assertFalse(gms.wanted(False, have, names))

    def test_uses_firebase(self):
        self.assertTrue(gms.uses_firebase(["com.google.firebase:firebase-auth"]))
        self.assertFalse(gms.uses_firebase(["com.google.android.gms:play-services-maps", "com.google.firebase:protolite-well-known-types"]))
        self.assertFalse(gms.uses_firebase([]))

    def test_crashlytics_wanted_truth_table(self):
        for setting in (None, "auto"):
            for name, expect in (("com.google.firebase:firebase-crashlytics", True), ("com.google.firebase:firebase-crashlytics-ktx", True), ("com.google.firebase:firebase-crashlytics-ndk", True),
                                 ("com.google.firebase:firebase-analytics", False), ("com.acme:firebase-crashlytics", False)):
                self.assertEqual(gms.crashlytics_wanted(setting, [name]), expect, (setting, name))
            self.assertFalse(gms.crashlytics_wanted(setting, []))
        self.assertTrue(gms.crashlytics_wanted(True, []))
        self.assertFalse(gms.crashlytics_wanted(False, ["com.google.firebase:firebase-crashlytics"]))

    def test_mapping_id(self):
        base = {"applicationId": PKG, "versionCode": 1, "cfg": {"minify": True, "javacArgs": []}, "src": "aa", "deps": ["d1", "d2"], "r8": "x"}
        mid = gms.mapping_id(base)
        self.assertRegex(mid, r"^[0-9a-f]{32}$")
        self.assertEqual(gms.mapping_id(copy.deepcopy(base)), mid)                       # deterministic
        self.assertEqual(gms.mapping_id(dict(reversed(list(base.items())))), mid)       # key order does not matter
        seen = {mid}
        for change in (lambda d: d.update(applicationId="com.acme.x"), lambda d: d.update(versionCode=2), lambda d: d["cfg"].update(minify=False), lambda d: d["cfg"]["javacArgs"].append("-Xlint"),
                       lambda d: d.update(src="ab"), lambda d: d["deps"].reverse(), lambda d: d["deps"].append("d3"), lambda d: d.update(r8="y"), lambda d: d.update(extra=1)):
            d = copy.deepcopy(base)
            change(d)
            seen.add(gms.mapping_id(d))
        self.assertEqual(len(seen), 10, "every input changes the id")

    def test_mapping_xml(self):
        text = gms.mapping_xml(MID)
        self.assertEqual(strings_of(text), {"com.google.firebase.crashlytics.mapping_file_id": MID})
        self.assertEqual(gms.MAPPING_ID_NAME, "com.google.firebase.crashlytics.mapping_file_id")


# =============================================================================================================== the records and the upload
class OutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="apkforge-gms-out-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "mapping")

    def made(self, stem="app", mid=MID, app_id=APP_ID):
        os.makedirs(self.out, exist_ok=True)
        write(os.path.join(self.out, stem + "-mapping.txt"), "# compiler: R8\n")
        gms.write_outputs(self.out, stem, mid, app_id, PKG, "1.2", 7)

    def test_round_trip(self):
        self.made()
        rec = gms.read_outputs(self.out)
        self.assertEqual(rec, {"mappingId": MID, "mappingFile": "app-mapping.txt", "resourceFile": "app-crashlytics.xml", "firebaseAppId": APP_ID, "applicationId": PKG,
                               "versionName": "1.2", "versionCode": 7, "dir": self.out})
        self.assertEqual(gms.read_outputs(self.out, ":app"), rec)
        self.assertEqual(gms.read_outputs(self.out, "app"), rec)
        with open(os.path.join(self.out, "app-crashlytics.xml"), encoding="utf-8") as f:
            self.assertEqual(f.read(), gms.mapping_xml(MID))                              # the resource file is the one the app carries
        with open(os.path.join(self.out, "app-crashlytics.json"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(list(json.loads(text)), sorted(json.loads(text)))                # sorted keys: the same record is the same bytes
        self.assertTrue(text.endswith("\n"))

    def test_a_record_without_a_firebase_app_id(self):
        self.made(app_id=None)
        self.assertIsNone(gms.read_outputs(self.out)["firebaseAppId"])

    def test_remove_outputs(self):
        self.made()
        gms.remove_outputs(self.out, "app")
        self.assertEqual(sorted(os.listdir(self.out)), ["app-mapping.txt"])               # the mapping itself stays
        gms.remove_outputs(self.out, "app")                                                # and a second call, or a folder that is not there, is fine
        gms.remove_outputs(os.path.join(self.tmp, "nowhere"), "app")
        with self.assertRaises(ForgeError):
            gms.read_outputs(self.out)

    def test_nothing_to_upload(self):
        with self.assertRaisesRegex(ForgeError, "nothing to upload"):
            gms.read_outputs(self.out)
        self.made()
        with self.assertRaisesRegex(ForgeError, r"nothing to upload.*module :lib"):
            gms.read_outputs(self.out, ":lib")

    def test_several_modules_need_a_choice(self):
        self.made("app")
        self.made("wear")
        with self.assertRaisesRegex(ForgeError, r"several modules.*app.*wear.*--module"):
            gms.read_outputs(self.out)
        self.assertEqual(gms.read_outputs(self.out, ":wear")["resourceFile"], "wear-crashlytics.xml")

    def test_a_record_that_names_a_missing_file(self):
        self.made()
        os.remove(os.path.join(self.out, "app-crashlytics.xml"))
        with self.assertRaisesRegex(ForgeError, r"app-crashlytics\.json names app-crashlytics\.xml, which is not in"):
            gms.read_outputs(self.out)
        self.made()
        os.remove(os.path.join(self.out, "app-mapping.txt"))
        with self.assertRaisesRegex(ForgeError, "names app-mapping.txt"):
            gms.read_outputs(self.out)

    def test_bad_json_and_json_that_is_not_a_record(self):
        self.made()
        path = os.path.join(self.out, "app-crashlytics.json")
        write(path, "{broken")
        with self.assertRaisesRegex(ForgeError, "not valid JSON"):
            gms.read_outputs(self.out)
        good = {"mappingId": MID, "mappingFile": "app-mapping.txt", "resourceFile": "app-crashlytics.xml"}
        for bad in ([], [good], "text", 7, None, {}, dict(good, mappingId=""), dict(good, mappingId=5), dict(good, mappingFile=["x"]), dict(good, resourceFile=None),
                    {k: v for k, v in good.items() if k != "mappingFile"}):
            write(path, json.dumps(bad))
            with self.assertRaisesRegex(ForgeError, "is not a record apkforge wrote", msg=repr(bad)):                # a ForgeError, not a TypeError or KeyError later
                gms.read_outputs(self.out)
        write(path, json.dumps(good))
        self.assertEqual(gms.read_outputs(self.out)["mappingId"], MID)

    def test_upload_command(self):
        self.made()
        rec = gms.read_outputs(self.out)
        self.assertEqual(gms.upload_command("/opt/bin/firebase", rec, APP_ID),
                         ["/opt/bin/firebase", "crashlytics:mappingfile:upload", "--app=" + APP_ID, "--resource-file=" + os.path.join(self.out, "app-crashlytics.xml"),
                          os.path.join(self.out, "app-mapping.txt")])

    def test_find_firebase(self):
        prog = write(os.path.join(self.tmp, "bin", "firebase"), "#!/bin/sh\n", 0o755)
        self.assertEqual(gms.find_firebase(prog), prog)                                    # an explicit file
        with self.assertRaisesRegex(ForgeError, "--firebase .*nowhere: no such program"):
            gms.find_firebase(os.path.join(self.tmp, "nowhere"))
        with mock.patch.object(util, "which", lambda n: "/usr/local/bin/" + n if n in ("firebase", "fb") else None):
            self.assertEqual(gms.find_firebase(), "/usr/local/bin/firebase")
            self.assertEqual(gms.find_firebase("fb"), "/usr/local/bin/fb")                # an explicit name is looked up on the PATH
            with self.assertRaises(ForgeError):
                gms.find_firebase("missing")
        with mock.patch.object(util, "which", lambda n: None):
            with self.assertRaisesRegex(ForgeError, "Firebase CLI is not installed.*firebase login"):
                gms.find_firebase()


# =================================================================================================================== the setting
class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="apkforge-gms-cfg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_defaults_and_values_that_load(self):
        self.assertEqual((project.DEFAULTS["googleServices"], project.DEFAULTS["crashlytics"]), (None, None))
        for v in (True, False, "auto", None):
            m = project.Module(":app", self.tmp, {"googleServices": v, "crashlytics": v})
            self.assertEqual((m.googleServices, m.crashlytics), (v, v))
        m = project.Module(":app", self.tmp, {})
        self.assertEqual((m.googleServices, m.crashlytics), (None, None))

    def test_values_that_are_errors(self):
        """the string "false" is not empty: taken as a truth value it would switch the feature ON"""
        for key in ("googleServices", "crashlytics"):
            for bad in ("false", "true", "yes", "AUTO", 1, 0, [], {}):
                with self.assertRaisesRegex(ForgeError, r'"%s" must be true, false or "auto"' % key, msg=repr(bad)):
                    project.Module(":app", self.tmp, {key: bad})


# ======================================================================================================== the build steps, on stand-ins
class StepCase(unittest.TestCase):
    """Builder.google_services, Builder.crashlytics_plan and Builder.generated_files, run without a build: a real Module (made by templates.create), types.SimpleNamespace for the Builder and for the
    Prep, real deps.Artifact objects for the dependencies (`.name` is "group:artifact" and fingerprint() needs no files), and the messages caught instead of printed.
    util.sha256_file caches by (path, size, mtime_ns): a test that rewrites a file changes its size, so that no hash can be stale"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="apkforge-gms-step-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proj = os.path.join(self.tmp, "app")
        with quiet():
            templates.create(self.proj, "java", None, PKG)
        self.base_cfg = read_json(os.path.join(self.proj, "apkforge.json"))
        self.msgs = {"info": [], "warn": [], "detail": []}
        for kind, bucket in self.msgs.items():
            p = mock.patch.object(B, kind, bucket.append)
            p.start()
            self.addCleanup(p.stop)

    def module(self, **cfg):
        """the app module with these settings on top of the template's own (not on top of the settings of an earlier call)"""
        path = os.path.join(self.proj, "apkforge.json")
        write(path, json.dumps(dict(self.base_cfg, **cfg)))
        return project.load(self.proj).default_app()

    def builder(self, variant="debug", tools="1"):
        return types.SimpleNamespace(variant=variant, release=variant == "release", tc=types.SimpleNamespace(ident=lambda what: "%s-%s" % (what, tools)))

    @staticmethod
    def prep(*names, compile_only=()):
        return types.SimpleNamespace(runtime=[deps.Artifact(n) for n in names], compile_only=list(compile_only), gms=None, crashlytics=None)

    def put_json(self, rel="google-services.json", data=None):
        return write(os.path.join(self.proj, *rel.split("/")), json.dumps(data or gs_data()))

    def quiet_messages(self):
        return {k: v for k, v in self.msgs.items() if v and k != "detail"}


class GoogleServicesStepTests(StepCase):
    def run_step(self, m, pre, variant="debug", appid=PKG):
        B.Builder.google_services(self.builder(variant), m, pre, appid)
        return pre

    def test_auto_with_the_file_and_a_firebase_dependency_processes_it(self):
        self.put_json()
        pre = self.run_step(self.module(), self.prep("com.google.firebase:firebase-analytics"))
        self.assertEqual(pre.gms.values, EXPECTED)
        self.assertEqual(self.msgs["warn"], [])
        self.assertTrue(any("google-services.json -> 10 string resources for %s" % PKG in m and APP_ID in m for m in self.msgs["info"]), self.msgs["info"])
        pre = self.run_step(self.module(googleServices="auto"), self.prep("com.google.android.gms:play-services-maps"))      # a Play services dependency counts as well
        self.assertIsNotNone(pre.gms)

    def test_the_file_without_a_dependency_is_only_said(self):
        self.put_json()
        pre = self.run_step(self.module(), self.prep("androidx.core:core"))
        self.assertIsNone(pre.gms)
        self.assertEqual(self.msgs["warn"], [])
        self.assertEqual(len(self.msgs["info"]), 1)
        self.assertIn("is there, but nothing here depends on Firebase or Play services", self.msgs["info"][0])
        self.assertIn('"googleServices": true', self.msgs["info"][0])

    def test_a_firebase_dependency_without_the_file_warns_and_says_where_it_looked(self):
        pre = self.run_step(self.module(), self.prep("com.google.firebase:firebase-auth"))
        self.assertIsNone(pre.gms)
        self.assertEqual(len(self.msgs["warn"]), 1)
        for place in gms.json_locations("debug"):
            self.assertIn(place, self.msgs["warn"][0])
        self.assertIn("FirebaseApp.initializeApp() fails at run time", self.msgs["warn"][0])
        self.assertEqual(self.msgs["info"], [])

    def test_a_play_services_dependency_without_the_file_is_not_worth_a_warning(self):
        self.run_step(self.module(), self.prep("com.google.android.gms:play-services-maps"))
        self.assertEqual(self.quiet_messages(), {})

    def test_true_without_the_file_is_an_error(self):
        with self.assertRaisesRegex(ForgeError, r'"googleServices" is true and there is no google-services.json \(looked for: src/debug/google-services.json'):
            self.run_step(self.module(googleServices=True), self.prep())

    def test_true_processes_the_file_without_any_dependency(self):
        self.put_json()
        self.assertEqual(self.run_step(self.module(googleServices=True), self.prep()).gms.values, EXPECTED)

    def test_false_is_silent(self):
        self.put_json()
        pre = self.run_step(self.module(googleServices=False), self.prep("com.google.firebase:firebase-analytics"))
        self.assertIsNone(pre.gms)
        self.assertEqual(self.quiet_messages(), {})
        self.run_step(self.module(googleServices=False), self.prep("com.google.firebase:firebase-analytics"), variant="release")
        self.assertEqual(self.quiet_messages(), {})

    def test_the_variant_picks_the_file(self):
        self.put_json("google-services.json", dict(gs_data(), project_info=dict(gs_data()["project_info"], project_id="from-the-module-root")))
        self.put_json("src/debug/google-services.json", dict(gs_data(), project_info=dict(gs_data()["project_info"], project_id="from-src-debug")))
        m = self.module(googleServices=True)
        self.assertEqual(self.run_step(m, self.prep(), "debug").gms.values["project_id"], "from-src-debug")
        self.assertEqual(self.run_step(m, self.prep(), "release").gms.values["project_id"], "from-the-module-root")       # a release build ignores src/debug

    def test_a_file_for_another_package_is_an_error_that_names_it(self):
        path = self.put_json("google-services.json", gs_data(package="com.acme.elsewhere"))
        with self.assertRaises(ForgeError) as cm:
            self.run_step(self.module(googleServices=True), self.prep(), appid="com.acme.wanted")
        self.assertIn(path, str(cm.exception))
        self.assertIn("com.acme.wanted", str(cm.exception))
        self.assertIn("com.acme.elsewhere", str(cm.exception))

    def test_a_service_with_an_unknown_status_warns_through_the_build(self):
        d = gs_data()
        mine(d)["services"]["maps_service"]["status"] = 9
        self.put_json(data=d)
        self.run_step(self.module(googleServices=True), self.prep())
        self.assertEqual(len(self.msgs["warn"]), 1)
        self.assertIn("maps_service", self.msgs["warn"][0])
        self.assertIn("google-services.json", self.msgs["warn"][0])

    def test_an_earlier_result_does_not_survive_a_module_that_does_not_want_it(self):
        self.put_json()
        m = self.module(googleServices=True)
        pre = self.run_step(m, self.prep())
        self.assertIsNotNone(pre.gms)
        self.run_step(self.module(googleServices=False), pre)
        self.assertIsNone(pre.gms)


class GeneratedFilesTests(StepCase):
    def files(self, m, pre):
        return B.Builder.generated_files(self.builder(), m, pre)

    def test_nothing_generated(self):
        self.assertEqual(self.files(self.module(), self.prep()), {})

    def test_res_values_alone(self):
        files = self.files(self.module(resValues={"gen_marker": "hello"}), self.prep())
        self.assertEqual(list(files), ["values/generated.xml"])
        self.assertEqual(strings_of(files["values/generated.xml"]), {"gen_marker": "hello"})

    def test_a_res_value_beats_the_same_name_from_google_services(self):
        self.put_json()
        m = self.module(googleServices=True, resValues={"firebase_database_url": "https://mine.example", "own_name": "x"})
        pre = self.prep()
        B.Builder.google_services(self.builder(), m, pre, PKG)
        files = self.files(m, pre)
        own, theirs = strings_of(files["values/generated.xml"]), strings_of(files[gms.VALUES_FILE])
        self.assertEqual(own["firebase_database_url"], "https://mine.example")
        self.assertNotIn("firebase_database_url", theirs)
        self.assertEqual(theirs, {k: v for k, v in EXPECTED.items() if k != "firebase_database_url"})
        self.assertEqual(set(own) & set(theirs), set(), "a string defined twice is an aapt2 error")
        self.assertIn(gms.TRACKER_FILE, files)

    def test_the_mapping_id_is_a_resource(self):
        pre = self.prep()
        pre.crashlytics = {"id": MID}
        files = self.files(self.module(), pre)
        self.assertEqual(strings_of(files["values/crashlytics_mapping_id.xml"]), {gms.MAPPING_ID_NAME: MID})


class CrashlyticsPlanTests(StepCase):
    CRASH = "com.google.firebase:firebase-crashlytics"

    def plan(self, m=None, pre=None, variant="release", tools="1", appid=PKG):
        m = m or self.module(minify=True, crashlytics=True)
        pre = pre or self.prep()
        B.Builder.crashlytics_plan(self.builder(variant, tools), m, pre, appid)
        return pre

    def mid(self, **kw):
        c = self.plan(**kw).crashlytics
        self.assertIsNotNone(c)
        return c["id"]

    def test_a_release_build_with_minify_gets_an_id(self):
        self.put_json()
        m = self.module(minify=True, versionName="2.5", versionCode=11, googleServices=True)
        pre = self.prep(self.CRASH)
        B.Builder.google_services(self.builder("release"), m, pre, PKG)
        self.plan(m, pre)
        c = pre.crashlytics
        self.assertRegex(c["id"], r"^[0-9a-f]{32}$")
        self.assertEqual((c["app_id"], c["application_id"], c["version_name"], c["version_code"]), (APP_ID, PKG, "2.5", 11))
        self.assertTrue(any(c["id"] in m_ for m_ in self.msgs["info"]), self.msgs["info"])
        self.assertEqual(self.msgs["warn"], [])

    def test_without_google_services_there_is_no_firebase_app_id(self):
        self.assertIsNone(self.plan().crashlytics["app_id"])

    def test_auto_follows_the_dependency(self):
        m = self.module(minify=True)
        self.assertIsNotNone(self.plan(m, self.prep(self.CRASH + "-ktx")).crashlytics)
        self.assertEqual(len(self.msgs["info"]), 1)                                    # it is said once: the id, and the command that uploads under it
        self.msgs["info"].clear()
        self.assertIsNone(self.plan(m, self.prep("com.google.firebase:firebase-analytics")).crashlytics)
        self.assertEqual(self.quiet_messages(), {})                                    # nobody asked for it, so no word about it

    def test_false_is_silent(self):
        self.assertIsNone(self.plan(self.module(minify=True, crashlytics=False), self.prep(self.CRASH)).crashlytics)
        self.assertEqual(self.quiet_messages(), {})

    def test_no_id_without_r8_or_in_a_debug_build(self):
        self.assertIsNone(self.plan(variant="debug").crashlytics)                        # debug: no mapping
        self.assertIsNone(self.plan(self.module(minify=False, crashlytics=True)).crashlytics)      # release without minify: no mapping either
        self.assertTrue(any("no R8 mapping in this build" in m for m in self.msgs["info"]), self.msgs)      # said when the person asked for it ("crashlytics": true) ...
        self.msgs["info"].clear()
        self.msgs["detail"].clear()
        self.plan(self.module(minify=False), self.prep(self.CRASH))                         # ... and only in the log when it was automatic
        self.assertEqual(self.msgs["info"], [])
        self.assertTrue(any("no R8 mapping in this build" in m for m in self.msgs["detail"]))

    def test_a_res_value_for_the_id_name_wins(self):
        m = self.module(minify=True, crashlytics=True, resValues={gms.MAPPING_ID_NAME: "mine"})
        self.assertIsNone(self.plan(m).crashlytics)
        self.assertTrue(any("resValues" in x and gms.MAPPING_ID_NAME in x for x in self.msgs["info"]))

    def test_the_same_inputs_give_the_same_id(self):
        self.assertEqual(self.mid(), self.mid())
        self.assertEqual(self.mid(m=self.module(minify=True, crashlytics=True)), self.mid(m=self.module(minify=True, crashlytics=True)))

    def test_every_input_changes_the_id(self):
        base = self.mid()
        java = os.path.join(self.proj, "src", "main", "java", "com", "acme", "gms", "MainActivity.java")
        ids = {"base": base}
        ids["appid"] = self.mid(appid="com.acme.other")
        ids["versionCode"] = self.mid(m=self.module(minify=True, crashlytics=True, versionCode=99))
        ids["versionName"] = self.mid(m=self.module(minify=True, crashlytics=True, versionName="9.9.9"))
        ids["config"] = self.mid(m=self.module(minify=True, crashlytics=True, javacArgs=["-Xlint:all"]))
        ids["tools"] = self.mid(tools="2")
        ids["dependency"] = self.mid(pre=self.prep("androidx.core:core"))
        with open(java, "a", encoding="utf-8") as f:
            f.write("// a change that is longer than the one before\n")
        ids["source"] = self.mid()
        with open(os.path.join(self.proj, "src", "main", "res", "values", "strings.xml"), "a", encoding="utf-8") as f:
            f.write("<!-- resources changed, and longer -->\n")
        ids["resource"] = self.mid()
        with open(os.path.join(self.proj, "src", "main", "AndroidManifest.xml"), "a", encoding="utf-8") as f:
            f.write("<!-- manifest changed, and longer still -->\n")
        ids["manifest"] = self.mid()
        self.assertEqual(len(set(ids.values())), len(ids), ids)

    def test_a_changed_compile_only_artifact_changes_the_id(self):
        """R8 gets compile-only jars as --classpath, so what they hold decides what it sees: the cache key of dex() hashes them, and so must the id"""
        jar = write(os.path.join(self.tmp, "stub.jar"), "one")
        co = deps.Artifact("com.acme:api")
        co.jars = [jar]
        first = self.mid(pre=self.prep(compile_only=[co]))
        self.assertEqual(self.mid(pre=self.prep(compile_only=[co])), first)
        write(jar, "twotwo")                                                                # a different size: the hash cache is keyed by size and mtime
        self.assertNotEqual(self.mid(pre=self.prep(compile_only=[co])), first)
        self.assertNotEqual(self.mid(pre=self.prep()), first)

    def test_a_changed_proguard_file_changes_the_id(self):
        rules = write(os.path.join(self.proj, "rules.pro"), "-keep class a.B\n")
        m = self.module(minify=True, crashlytics=True, proguardFiles=["rules.pro"])
        first = self.mid(m=m)
        write(rules, "-keep class a.B\n-keep class c.D\n")
        self.assertNotEqual(self.mid(m=m), first)

    def test_a_changed_signing_section_does_not_change_the_id(self):
        """keys and passwords do not change what R8 sees, so rotating a key must not change the mapping id (the demo build below does it with a real new key)"""
        plain = self.mid(m=self.module(minify=True, crashlytics=True))
        one = self.mid(m=self.module(minify=True, crashlytics=True, signing={"keystore": "a.p12", "alias": "a", "storePasswordEnv": "PW_A"}))
        two = self.mid(m=self.module(minify=True, crashlytics=True, signing={"keystore": "other/b.p12", "alias": "b", "storePasswordEnv": "PW_B", "keyPasswordEnv": "PW_C"},
                                     uploadSigning={"keystore": "up.p12", "alias": "up"}))
        self.assertEqual({plain, one, two}, {plain})
        self.assertNotEqual(self.mid(m=self.module(minify=True, crashlytics=True, signing=None, javacArgs=["-g"])), plain)       # the control: another key of the config does


# ================================================================================================================ crashlytics-upload
FAKE_FIREBASE = r'''#!/usr/bin/env bash
# a stand-in for the Firebase CLI: writes its arguments, one per line, to $FAKE_FB_DIR/args.txt, and exits with $FAKE_FB_RC
printf '%s\n' "$@" > "${FAKE_FB_DIR:?}/args.txt"
echo "fake firebase: $1"
exit "${FAKE_FB_RC:-0}"
'''


class UploadCommandTests(unittest.TestCase):
    """`apkforge crashlytics-upload`, on a hand-made build/outputs/mapping/ (what a release build writes) and a stand-in for the Firebase CLI"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="apkforge-gms-upload-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proj = os.path.join(self.tmp, "app")
        with quiet():
            templates.create(self.proj, "java", None, PKG)
        self.mapping = os.path.join(self.proj, "build", "outputs", "mapping")
        self.fb = write(os.path.join(self.tmp, "fb", "firebase"), FAKE_FIREBASE, 0o755)
        self.fbdir = os.path.dirname(self.fb)

    def record(self, stem="app", app_id=APP_ID):
        write(os.path.join(self.mapping, stem + "-mapping.txt"), "# compiler: R8\n")
        gms.write_outputs(self.mapping, stem, MID, app_id, PKG, "1.0", 1)

    def args(self):
        with open(os.path.join(self.fbdir, "args.txt"), encoding="utf-8") as f:
            return f.read().splitlines()

    def expected(self, app_id=APP_ID, stem="app"):
        return ["crashlytics:mappingfile:upload", "--app=" + app_id, "--resource-file=" + os.path.join(self.mapping, stem + "-crashlytics.xml"), os.path.join(self.mapping, stem + "-mapping.txt")]

    def test_dry_run_prints_the_command_and_runs_nothing(self):
        self.record()
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--dry-run", "--firebase", self.fb, env={"FAKE_FB_DIR": self.fbdir})
        self.assertEqual(rc, 0, err)
        self.assertEqual(shlex.split(out.strip().splitlines()[-1]), [self.fb] + self.expected())
        self.assertFalse(os.path.exists(os.path.join(self.fbdir, "args.txt")))
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--dry-run", env={"PATH": os.path.join(self.tmp, "empty")})        # it needs no Firebase CLI: it is what you run elsewhere
        self.assertEqual(rc, 0, err)
        self.assertEqual(shlex.split(out.strip().splitlines()[-1]), ["firebase"] + self.expected())

    def test_the_real_call(self):
        self.record()
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--firebase", self.fb, env={"FAKE_FB_DIR": self.fbdir})
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.args(), self.expected())
        self.assertIn("uploaded the mapping of %s 1.0 to Crashlytics under the mapping id %s" % (PKG, MID), out)

    def test_app_overrides_the_record(self):
        self.record()
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--firebase", self.fb, "--app", "1:9:android:override", env={"FAKE_FB_DIR": self.fbdir})
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.args(), self.expected(app_id="1:9:android:override"))

    def test_a_record_without_a_firebase_app_id_needs_app(self):
        self.record(app_id=None)
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--firebase", self.fb, env={"FAKE_FB_DIR": self.fbdir})
        self.assertEqual(rc, 1)
        self.assertIn("--app", err)
        self.assertFalse(os.path.exists(os.path.join(self.fbdir, "args.txt")))
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--firebase", self.fb, "--app", APP_ID, env={"FAKE_FB_DIR": self.fbdir})
        self.assertEqual(rc, 0, err)

    def test_a_missing_firebase_cli_is_a_clear_error(self):
        self.record()
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        rc, out, err = run_cli("crashlytics-upload", self.proj, env={"PATH": empty})
        self.assertEqual(rc, 1)
        self.assertIn("the Firebase CLI is not installed", err)
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--firebase", os.path.join(self.tmp, "nowhere"))
        self.assertEqual(rc, 1)
        self.assertIn("--firebase", err)

    def test_a_failing_firebase_cli_fails_the_command(self):
        self.record()
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--firebase", self.fb, env={"FAKE_FB_DIR": self.fbdir, "FAKE_FB_RC": "3"})
        self.assertEqual(rc, 1)
        self.assertIn("firebase crashlytics:mappingfile:upload failed (exit 3)", err)
        self.assertNotIn("uploaded the mapping", out)

    def test_nothing_to_upload(self):
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--firebase", self.fb)
        self.assertEqual(rc, 1)
        self.assertIn("nothing to upload", err)

    def test_several_modules_need_module(self):
        self.record("app")
        self.record("wear")
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("--module", err)
        rc, out, err = run_cli("crashlytics-upload", self.proj, "--dry-run", "--module", ":wear")
        self.assertEqual(rc, 0, err)
        self.assertEqual(shlex.split(out.strip().splitlines()[-1]), ["firebase"] + self.expected(stem="wear"))

    def test_the_command_is_in_the_quick_start(self):
        rc, out, err = run_cli()                                                         # a bare `apkforge` prints the quick start
        self.assertIn("crashlytics-upload", out + err)
        self.assertIn("crashlytics-upload", cli.QUICKSTART)


# ============================================================================================================== import-gradle
MANIFEST = '<manifest xmlns:android="http://schemas.android.com/apk/res/android"><application/></manifest>'
KTS_ANDROID = '''android {
    namespace = "com.acme.fb"
    compileSdk = 35
    defaultConfig { applicationId = "com.acme.fb"; minSdk = 24; targetSdk = 35; versionCode = 1; versionName = "1.0" }
}
dependencies { implementation("com.google.firebase:firebase-analytics:22.0.0") }
'''
GROOVY_ANDROID = """android {
    namespace 'com.acme.fb'
    compileSdk 35
    defaultConfig { applicationId 'com.acme.fb'; minSdk 24; targetSdk 35; versionCode 1; versionName '1.0' }
}
dependencies { implementation 'com.google.firebase:firebase-analytics:22.0.0' }
"""
KTS_SETTINGS = 'rootProject.name = "fb"\ninclude(":app")\n'
GROOVY_SETTINGS = "rootProject.name = 'fb'\ninclude ':app'\n"
CATALOG = '''[versions]
agp = "8.5.0"
gms = "4.4.2"
[plugins]
android-application = { id = "com.android.application", version.ref = "agp" }
google-services = { id = "com.google.gms.google-services", version.ref = "gms" }
firebase-crashlytics = { id = "com.google.firebase.crashlytics", version = "3.0.2" }
'''
LAYOUTS = {
    "Kotlin DSL, plugins {}": {
        "settings.gradle.kts": KTS_SETTINGS,
        "app/build.gradle.kts": 'plugins {\n    id("com.android.application")\n    id("com.google.gms.google-services")\n    id("com.google.firebase.crashlytics")\n}\n' + KTS_ANDROID},
    "Groovy, plugins {}": {
        "settings.gradle": GROOVY_SETTINGS,
        "app/build.gradle": "plugins {\n    id 'com.android.application'\n    id 'com.google.gms.google-services'\n    id 'com.google.firebase.crashlytics'\n}\n" + GROOVY_ANDROID},
    "Groovy, apply plugin:": {
        "settings.gradle": GROOVY_SETTINGS,
        "app/build.gradle": "apply plugin: 'com.android.application'\napply plugin: 'com.google.gms.google-services'\napply plugin: 'com.google.firebase.crashlytics'\n" + GROOVY_ANDROID},
    "a root script with apply false, the module with version catalog aliases": {
        "settings.gradle.kts": KTS_SETTINGS,
        "build.gradle.kts": 'plugins {\n    id("com.android.application") version "8.5.0" apply false\n    id("com.google.gms.google-services") version "4.4.2" apply false\n'
                            '    id("com.google.firebase.crashlytics") version "3.0.2" apply false\n}\n',
        "gradle/libs.versions.toml": CATALOG,
        "app/build.gradle.kts": "plugins {\n    alias(libs.plugins.android.application)\n    alias(libs.plugins.google.services)\n    alias(libs.plugins.firebase.crashlytics)\n}\n" + KTS_ANDROID},
}


class ImporterTests(unittest.TestCase):
    """import-gradle used to list com.google.gms.google-services and com.google.firebase.crashlytics as NOT MAPPED; apkforge does both now"""

    def imported(self, files):
        d = tempfile.mkdtemp(prefix="apkforge-gms-import-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for rel, text in dict(files, **{"app/src/main/AndroidManifest.xml": MANIFEST}).items():
            write(os.path.join(d, *rel.split("/")), text)
        return gradle_import.import_project(d, write=False)

    def test_the_two_plugins_in_every_way_gradle_applies_them(self):
        for name, files in LAYOUTS.items():
            with self.subTest(name):
                r = self.imported(files)
                app = r["config"]["modules"]["app"]
                self.assertIs(app.get("googleServices"), True)
                self.assertIs(app.get("crashlytics"), True)
                self.assertEqual(r["stats"]["unmapped"], 0)
                self.assertEqual(r["notes"], [])

    def test_one_plugin_maps_only_its_own_setting(self):
        files = {"settings.gradle.kts": KTS_SETTINGS, "app/build.gradle.kts": 'plugins {\n    id("com.android.application")\n    id("com.google.gms.google-services")\n}\n' + KTS_ANDROID}
        app = self.imported(files)["config"]["modules"]["app"]
        self.assertIs(app.get("googleServices"), True)
        self.assertNotIn("crashlytics", app)
        files["app/build.gradle.kts"] = 'plugins {\n    id("com.android.application")\n}\n' + KTS_ANDROID
        app = self.imported(files)["config"]["modules"]["app"]
        self.assertNotIn("googleServices", app)
        self.assertNotIn("crashlytics", app)

    def test_the_plugin_tables(self):
        for plugin, key in (("com.google.gms.google-services", "googleServices"), ("com.google.firebase.crashlytics", "crashlytics")):
            self.assertEqual(gradle_import.PLUGIN_MAPPED[plugin], key)
            self.assertNotIn(plugin, gradle_import.PLUGIN_UNSUPPORTED)
            self.assertIn(key, project.DEFAULTS)                                           # what the importer writes is a setting apkforge reads


# ========================================================================================================================== UTF-8
class Utf8Tests(unittest.TestCase):
    """The generated res folder was written with open(path, "w"): in a locale whose encoding is not UTF-8 (LC_ALL=C on a CI machine, a Windows code page) a non-ASCII resValue was a
    UnicodeEncodeError, or mojibake that the XML's own `encoding="utf-8"` then contradicted.  Builder.overlay_zips writes every generated file as UTF-8 now.  The check runs in a
    subprocess, because a locale is set at start-up."""
    SCRIPT = textwrap.dedent('''
        import json, locale, os, sys, types
        sys.path.insert(0, sys.argv[1])
        from apkforge import build as B, project
        m = project.Module(":app", sys.argv[2], {"resValues": {"greeting": "caf\\u00e9 \\u2603 \\u00fc"}})
        seen = []
        me = types.SimpleNamespace(variant="debug", compile_res=lambda rd, label: seen.append(rd) or rd + ".zip")
        me.generated_files = types.MethodType(B.Builder.generated_files, me)
        B.Builder.overlay_zips(me, m, types.SimpleNamespace(), [], sys.argv[3], True)
        with open(os.path.join(seen[-1], "values", "generated.xml"), "rb") as f:
            print(json.dumps({"encoding": locale.getpreferredencoding(False), "bytes": f.read().hex()}))
    ''')

    def test_a_non_ascii_res_value_in_a_locale_that_is_not_utf8(self):
        tmp = tempfile.mkdtemp(prefix="apkforge-gms-utf8-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k not in ("LANG", "LC_CTYPE", "PYTHONIOENCODING")}
        env.update(LC_ALL="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
        p = subprocess.run([sys.executable, "-c", self.SCRIPT, SRC, tmp, os.path.join(tmp, "work")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace")[-1500:])
        res = json.loads(p.stdout.decode("ascii"))
        if res["encoding"].lower().replace("-", "") in ("utf8", "utf8mode"):
            self.skipTest("this Python cannot be made to use a locale that is not UTF-8, so the bug cannot be reproduced here")
        text = bytes.fromhex(res["bytes"]).decode("utf-8")                                 # the bytes on disk are UTF-8 whatever the locale was
        self.assertIn("caf\u00e9 \u2603 \u00fc", text)
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="utf-8"?>'))


# ============================================================================================================== the real thing
STUBS = {("com.google.firebase", "firebase-analytics", "22.0.0"): "Analytics", ("com.google.firebase", "firebase-crashlytics", "19.0.0"): "Crashlytics"}
STUB_POM = ('<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>%s</groupId><artifactId>%s</artifactId>'
            '<version>%s</version><packaging>jar</packaging></project>\n')


def make_stub_repo(tc, repo):
    """A local Maven repository with STAND-IN jars published as com.google.firebase:firebase-analytics:22.0.0 and firebase-crashlytics:19.0.0, one tiny class each.  They are not Google's SDK:
    they let a build run the auto-enable through the real resolver without the network.  Every jar needs a class of its own (two jars with the same class make R8 fail)."""
    tmp = tempfile.mkdtemp(prefix="stub-firebase-")
    try:
        for (g, a, v), cls in sorted(STUBS.items()):
            out = os.path.join(tmp, a, "classes")
            os.makedirs(out)
            java = write(os.path.join(tmp, a, "src", cls + ".java"), 'package com.google.firebase.fake;\npublic final class %s { public static String id() { return "%s"; } }\n' % (cls, a))
            util.run([tc.javac(), "--release", "8", "-d", out, java], what="javac (stand-in %s)" % a)
            d = os.path.join(repo, *g.split("."), a, v)
            os.makedirs(d)
            with zipfile.ZipFile(os.path.join(d, "%s-%s.jar" % (a, v)), "w", zipfile.ZIP_DEFLATED) as z:
                for r, _dirs, files in os.walk(out):
                    for f in sorted(files):
                        z.write(os.path.join(r, f), os.path.relpath(os.path.join(r, f), out).replace(os.sep, "/"))
            write(os.path.join(d, "%s-%s.pom" % (a, v)), STUB_POM % (g, a, v))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class DemoGmsTests(unittest.TestCase):
    """google-services.json and Crashlytics in real builds: a demo app built by apkforge, its resource table read back by aapt2 (the APK) and out of the zip (the bundle).
    Each build costs seconds (R8, bundletool and keytool are JVMs), so the scenarios are few and each one follows a project through several states."""

    @classmethod
    def setUpClass(cls):
        cls.tc = installed_toolchain()
        if cls.tc is None:
            raise unittest.SkipTest("aapt2, D8, bundletool and a JDK are not installed (apkforge doctor --bundletool)")
        cls.tmp = tempfile.mkdtemp(prefix="apkforge-gms-demo-")
        probe = os.path.join(cls.tmp, "probe")
        with quiet():
            templates.create(probe, "java", None, PKG)
        try:
            if not cls.tc.android_jar(project.load(probe).default_app().compileSdk):
                raise ForgeError("no android.jar")
        except ForgeError:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise unittest.SkipTest("the android.jar of the demo app is not installed")
        cls.pw = {"APKFORGE_STORE_PASS": "gms-test-pw-1"}
        cls.repo = os.path.join(cls.tmp, "repo")
        make_stub_repo(cls.tc, cls.repo)
        cls.firebase_deps = {"implementation": ["com.google.firebase:firebase-analytics:22.0.0", "com.google.firebase:firebase-crashlytics:19.0.0"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def make(self, name, json_file=True, **cfg):
        proj = os.path.join(self.tmp, name)
        with quiet():
            templates.create(proj, "java", None, PKG)
        self.set_cfg(proj, **cfg)
        if json_file:
            write(os.path.join(proj, "google-services.json"), json.dumps(gs_data()))
        return proj

    @staticmethod
    def set_cfg(proj, remove=(), **cfg):
        path = os.path.join(proj, "apkforge.json")
        new = {k: v for k, v in dict(read_json(path), **cfg).items() if k not in remove}
        write(path, json.dumps(new))

    def keygen(self, proj, *flags):
        with mock.patch.dict(os.environ, self.pw):
            rc, out, err = run_cli("keygen", proj, *flags)
        self.assertEqual(rc, 0, err[-1500:])

    def build(self, proj, *flags, env=None):
        rc, out, err = run_cli("build", proj, "--json", *flags, env=env)
        self.assertEqual(rc, 0, err[-1500:])
        res = json.loads(out)
        res["log"] = err                                                                # --json keeps stdout for the one document: the build's own messages are on stderr
        return res

    def dump(self, apk):
        rc, text = util.run([self.tc.aapt2(), "dump", "resources", apk], check=False)
        self.assertEqual(rc, 0, text[-500:])
        return text

    @staticmethod
    def bundle(aab):
        with zipfile.ZipFile(aab) as z:
            return z.namelist(), z.read("base/resources.pb")

    @staticmethod
    def said(res, fragment):
        return any(fragment in line for line in res["log"].splitlines())

    def test_a_debug_build_carries_the_strings_in_the_apk_and_in_the_bundle(self):
        proj = self.make("debug", googleServices=True, resValues={"firebase_database_url": "https://override.example"})
        res = self.build(proj, "--aab")
        dump = self.dump(res["apk"])
        for name in EXPECTED:
            self.assertIn("string/%s" % name, dump)
        self.assertIn(KEY, dump)                                                        # the apostrophe and the ampersand came through intact
        self.assertEqual(dump.count("string/firebase_database_url"), 1)                 # a resValues entry for a name of the file wins, and the name is there once
        self.assertIn("https://override.example", dump)
        self.assertNotIn("https://mydemoapp.firebaseio.com", dump)
        self.assertNotIn("mapping_file_id", dump)                                       # no Crashlytics here: no id
        with zipfile.ZipFile(res["apk"]) as z:
            self.assertIn("res/xml/global_tracker.xml", z.namelist())
        names, table = self.bundle(res["aab"])
        self.assertIn("base/res/xml/global_tracker.xml", names)
        for name, value in EXPECTED.items():                                            # the bundle has what the APK has (it used to miss everything the generated res folder held)
            self.assertIn(name.encode(), table, name)
            if name != "firebase_database_url":
                self.assertIn(value.encode(), table, name)
        self.assertIn(KEY.encode(), table)
        self.assertIn(b"https://override.example", table)
        self.assertNotIn(b"https://mydemoapp.firebaseio.com", table)
        self.assertFalse(os.path.exists(os.path.join(proj, "build", "outputs", "mapping")))
        self.assertTrue(self.said(res, "google-services.json -> 10 string resources"), res["log"][-1500:])

    def test_auto_enabled_release_build_one_mapping_id_everywhere_and_what_does_not_change_it(self):
        """no "googleServices" and no "crashlytics": the file is there and the module depends on firebase-analytics and firebase-crashlytics (stand-in jars in a local repository)"""
        proj = self.make("rel", minify=True, repositories=[self.repo], dependencies=self.firebase_deps)
        self.keygen(proj)
        res = self.build(proj, "--release", "--aab", env=self.pw)
        self.assertTrue(self.said(res, "google-services.json -> 10 string resources"), res["log"][-1500:])        # auto-enabled by the file and the dependency
        md = os.path.join(proj, "build", "outputs", "mapping")
        rec = read_json(os.path.join(md, "app-crashlytics.json"))
        mid = rec["mappingId"]
        self.assertRegex(mid, r"^[0-9a-f]{32}$")                                                           # auto-enabled by the dependency
        self.assertEqual((rec["firebaseAppId"], rec["applicationId"], rec["versionName"], rec["versionCode"], rec["mappingFile"], rec["resourceFile"]),
                         (APP_ID, PKG, "1.0", 1, "app-mapping.txt", "app-crashlytics.xml"))
        with open(os.path.join(md, "app-crashlytics.xml"), encoding="utf-8") as f:
            self.assertEqual(strings_of(f.read()), {gms.MAPPING_ID_NAME: mid})                            # the XML
        dump = self.dump(res["apk"])
        for name in EXPECTED:
            self.assertIn("string/%s" % name, dump)
        self.assertIn("string/" + gms.MAPPING_ID_NAME, dump)
        self.assertEqual(dump.count(mid), 1)                                                              # the APK
        self.assertIn(mid.encode(), self.bundle(res["aab"])[1])                                           # the bundle
        self.assertTrue(os.path.getsize(os.path.join(md, "app-mapping.txt")) > 0)
        apk_one = util.sha256_file(res["apk"])

        # the upload command reads what the build wrote: the app id of the google-services.json, the resource file that carries the id, the mapping
        rc, out, err = run_cli("crashlytics-upload", proj, "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertEqual(shlex.split(out.strip().splitlines()[-1]), ["firebase", "crashlytics:mappingfile:upload", "--app=" + APP_ID,
                                                                    "--resource-file=" + os.path.join(md, "app-crashlytics.xml"), os.path.join(md, "app-mapping.txt")])
        fbdir = os.path.join(self.tmp, "fb-rel")
        fb = write(os.path.join(fbdir, "firebase"), FAKE_FIREBASE, 0o755)
        rc, out, err = run_cli("crashlytics-upload", proj, "--firebase", fb, env={"FAKE_FB_DIR": fbdir})
        self.assertEqual(rc, 0, err)
        with open(os.path.join(fbdir, "args.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines()[1:3], ["--app=" + APP_ID, "--resource-file=" + os.path.join(md, "app-crashlytics.xml")])

        # "crashlytics": false wins over the dependency: both records go, the mapping stays, and the APK has no id
        self.set_cfg(proj, crashlytics=False)
        res = self.build(proj, "--release", env=self.pw)
        self.assertEqual(sorted(os.listdir(md)), ["app-mapping.txt"])
        self.assertNotIn("mapping_file_id", self.dump(res["apk"]))
        rc, out, err = run_cli("crashlytics-upload", proj, "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("nothing to upload", err)

        # a new key (another keystore, alias and "signing" section) is not another build of the code: the id stays, the APK does not
        self.set_cfg(proj, remove=("crashlytics",))
        self.keygen(proj, "--keystore", os.path.join(proj, "rotated.p12"), "--alias", "rotated", "--force")
        self.assertEqual(read_json(os.path.join(proj, "apkforge.json"))["signing"]["alias"], "rotated")
        res = self.build(proj, "--release", env=self.pw)
        self.assertEqual(read_json(os.path.join(md, "app-crashlytics.json"))["mappingId"], mid)
        self.assertEqual(self.dump(res["apk"]).count(mid), 1)
        self.assertNotEqual(util.sha256_file(res["apk"]), apk_one, "the signature should differ, or this proves nothing")

    def test_the_file_without_a_dependency_and_a_dependency_without_the_file(self):
        """auto-enable is conservative: the file alone is only mentioned, and a Firebase dependency without the file is a warning (the build goes on)"""
        res = self.build(self.make("filealone"))
        self.assertNotIn("string/google_app_id", self.dump(res["apk"]))
        self.assertTrue(self.said(res, "is there, but nothing here depends on Firebase or Play services"), res["log"][-1500:])
        res = self.build(self.make("depalone", json_file=False, repositories=[self.repo], dependencies=self.firebase_deps))
        self.assertNotIn("string/google_app_id", self.dump(res["apk"]))
        self.assertTrue(any("has no google-services.json" in l and "src/debug/google-services.json" in l for l in res["log"].splitlines()), res["log"][-1500:])
