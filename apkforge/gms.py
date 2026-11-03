"""google-services.json and Crashlytics, without Gradle.

Google's Gradle plugin `com.google.gms.google-services` turns google-services.json into string resources that the Firebase and Play services SDKs look up BY NAME at run time
(FirebaseOptions.fromResource reads google_app_id, google_api_key, gcm_defaultSenderId, ...).  This module does that step: which file to read, which `client` entry of it is this app,
and which resources come out.  The rules are those of the plugin's source (github.com/google/play-services-plugins, google-services-plugin: GoogleServicesPlugin.kt for the search
order, GoogleServicesTask.kt for the resources), not only those of Google's page about it.  That page leaves out google_storage_bucket and google_maps_key, which the plugin does
write, and it spells the analytics service with a hyphen where the file and the plugin have an underscore.

Crashlytics: the SDK finds the R8 mapping of a crash by one string resource, `com.google.firebase.crashlytics.mapping_file_id` (firebase-android-sdk, CommonUtils.getMappingFileId;
it only checks that the string is not empty).  The Gradle plugin makes a new id for every build and uploads the mapping under it.  Here the id is a hash of the build's inputs, because
an apkforge build is reproducible, and the upload is a command of its own (`apkforge crashlytics-upload`) that hands the mapping and the resource file to the Firebase CLI,
which is what Firebase documents for builds that do not use the Gradle plugin (`firebase crashlytics:mappingfile:upload --app --resource-file`).
"""
import glob
import hashlib
import json
import os
import posixpath

from . import util
from .util import ForgeError

JSON_NAME = "google-services.json"
VALUES_FILE = "values/google_services.xml"                   # inside the generated res folder; resValues of apkforge.json have values/generated.xml beside it
TRACKER_FILE = "xml/global_tracker.xml"                      # R.xml.global_tracker, for the Google Analytics v4 SDK
MAPPING_ID_NAME = "com.google.firebase.crashlytics.mapping_file_id"
FIREBASE_APP_PREFIX = "com.google.firebase:firebase-"
GMS_PREFIXES = ("com.google.firebase:", "com.google.android.gms:")
CRASHLYTICS_ARTIFACT = "com.google.firebase:firebase-crashlytics"      # also -ktx and -ndk

_STATUS_DISABLED, _STATUS_ENABLED, _CLIENT_TYPE_WEB = "1", "2", "3"


# ------------------------------------------------------------------------------------------------------- where the file is
def _cap(s):
    return s[:1].upper() + s[1:]


def json_locations(build_type, flavors=()):
    """Where the plugin looks for google-services.json for one variant, most specific first (GoogleServicesPlugin.getJsonLocations), as paths relative to the module
    directory: src/<flavor...>/<buildType>, src/<buildType>, ..., and last the module directory itself.  The first file that exists is the one.  `flavors` is the variant's
    flavor names, one per dimension; apkforge has no flavors yet, so callers pass none."""
    flavor = ""
    for f in flavors:
        flavor += f if not flavor else _cap(f)
    places = ["", "src/%s/%s" % (flavor, build_type), "src/%s/%s" % (build_type, flavor), "src/%s" % flavor, "src/%s" % build_type,
              "src/%s%s" % (flavor, _cap(build_type)), "src/%s" % build_type]
    sub = "src"
    for f in flavors:
        sub += "/" + f
        places += [sub, "%s/%s" % (sub, build_type), sub + _cap(build_type)]
    places = sorted(dict.fromkeys(places), key=lambda p: -p.count("/"))       # the plugin sorts by depth, deepest first; stable, like Kotlin's sortedByDescending
    return list(dict.fromkeys(posixpath.normpath(posixpath.join(p, JSON_NAME)) for p in places))


def find_json(module_dir, build_type, flavors=()):
    """-> (the path of the file the plugin would use, or None; every path that was looked at)"""
    cands = [os.path.join(module_dir, *p.split("/")) for p in json_locations(build_type, flavors)]
    for c in cands:
        if os.path.isfile(c):
            return c, cands
    return None, cands


# ----------------------------------------------------------------------------------------------------------- the file
class GoogleServices(object):
    """What one google-services.json says about one app: `values` = {resource name: string}, in the plugin's order (sorted by name), `tracker` = the Analytics tracking id that
    also goes into xml/global_tracker.xml (or None), `app_id` = the Firebase app id (google_app_id), `warnings` = what the plugin only logs."""

    def __init__(self, path, application_id, values, tracker, warnings):
        self.path, self.application_id, self.tracker, self.warnings = path, application_id, tracker, list(warnings)
        self.values = dict(sorted(values.items()))
        self.app_id = self.values["google_app_id"]

    def files(self, skip=()):
        """-> {path inside the generated res folder: text}.  `skip` = resource names that something else defines (a string twice is an aapt2 error)"""
        out = {VALUES_FILE: render_strings({k: v for k, v in self.values.items() if k not in skip})}
        if self.tracker:
            out[TRACKER_FILE] = render_strings({"ga_trackingId": self.tracker})
        return out


def _prim(obj, key):
    """the JSON primitive at obj[key] as text (Gson's JsonPrimitive.asString), or None for a missing key, null, an object or an array"""
    v = obj.get(key) if isinstance(obj, dict) else None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v) if isinstance(v, (str, int, float)) else None


def _clients(data):
    out = []
    for c in data.get("client") if isinstance(data.get("client"), list) else []:
        info = c.get("client_info") if isinstance(c, dict) else None
        android = info.get("android_client_info") if isinstance(info, dict) else None
        name = _prim(android, "package_name")
        if name is not None:
            out.append((name, c))
    return out


def _service(client, name, warnings):
    """client/services/<name> when that service is enabled (status 2); None when it is missing, disabled (1) or has a status the plugin does not know"""
    services = client.get("services")
    service = services.get(name) if isinstance(services, dict) else None
    status = _prim(service, "status")
    if status is None or status == _STATUS_DISABLED:
        return None
    if status != _STATUS_ENABLED:
        warnings.append("service %s has the status %s, which is unknown: it is left out (as the Google services plugin does)" % (name, status))
        return None
    return service


def _api_key(client):
    for k in client.get("api_key") if isinstance(client.get("api_key"), list) else []:
        cur = _prim(k, "current_key")
        if cur is not None:
            return cur
    return None


def parse(path, application_id):
    """google-services.json -> GoogleServices for the client whose package name is `application_id`.  Raises ForgeError with the file's path for what the plugin refuses:
    a file that is not JSON, a missing project_info / project_number / project_id / api_key / mobilesdk_app_id, and no client for the applicationId."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        raise ForgeError("%s is not valid JSON: %s" % (path, e))
    if not isinstance(data, dict):
        raise ForgeError("%s: malformed root json (an object is expected)" % path)
    info = data.get("project_info")
    if not isinstance(info, dict):
        raise ForgeError("%s: missing project_info object" % path)
    values, warnings = {}, []
    for key, res in (("project_number", "gcm_defaultSenderId"), ("project_id", "project_id")):
        v = _prim(info, key)
        if v is None:
            raise ForgeError("%s: missing project_info/%s" % (path, key))
        values[res] = v
    for key, res in (("storage_bucket", "google_storage_bucket"), ("firebase_url", "firebase_database_url")):
        v = _prim(info, key)
        if v is not None:
            values[res] = v
    clients = _clients(data)
    hit = next((c for name, c in clients if name == application_id), None)
    if hit is None:
        raise ForgeError("%s has no client for the applicationId %s (its clients: %s).  Add this app to the Firebase project and download the file again, or set \"applicationId\" "
                         "to a package the file knows" % (path, application_id, ", ".join(n for n, _ in clients) or "none"))
    tracker = None
    ga = _service(hit, "analytics_service", warnings)
    prop = ga.get("analytics_property") if ga else None
    if _prim(prop, "tracking_id") is not None:
        tracker = values["ga_trackingId"] = _prim(prop, "tracking_id")
    maps = _service(hit, "maps_service", warnings)
    key = _api_key(hit)
    if key is None:
        raise ForgeError("%s: the client %s has no api_key/current_key" % (path, application_id))       # the plugin refuses this too, with or without the maps service
    if maps:
        values["google_maps_key"] = key
    values["google_api_key"] = values["google_crash_reporting_api_key"] = key          # the second name is what older SDKs read
    app_id = _prim(hit.get("client_info"), "mobilesdk_app_id")
    if not app_id:
        raise ForgeError("%s: the client %s has no client_info/mobilesdk_app_id (a Google App Id).  Download a valid file from https://console.firebase.google.com" % (path, application_id))
    values["google_app_id"] = app_id
    oauth = hit.get("oauth_client")
    for c in oauth if isinstance(oauth, list) else []:
        if _prim(c, "client_type") == _CLIENT_TYPE_WEB and _prim(c, "client_id") is not None:
            values["default_web_client_id"] = _prim(c, "client_id")                # the first web client
            break
    return GoogleServices(path, application_id, values, tracker, warnings)


def xml_text(v):
    """a value as the text of a <string> element: aapt2's own escapes (backslash, quotes, a leading @ or ? that would make it a reference) and XML's"""
    v = v.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
    v = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "\\" + v if v and v[0] in "@?" else v                  # `v and`: "" in "@?" is True in Python, and an empty value must stay empty, not become a lone backslash


def render_strings(values):
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<resources>"]
    lines += ['    <string name="%s" translatable="false">%s</string>' % (k, xml_text(v)) for k, v in values.items()]
    return "\n".join(lines + ["</resources>"]) + "\n"


def wanted(setting, have_json, names):
    """Does this module process google-services.json?  `setting` is "googleServices" of apkforge.json: true / false win; None or "auto" = the file is there and the module compiles
    against a Firebase or Play services artifact (names = every group:artifact of its runtime classpath)."""
    if setting is None or setting == "auto":
        return bool(have_json and any(n.startswith(GMS_PREFIXES) for n in names))
    return bool(setting)


def uses_firebase(names):
    return any(n.startswith(FIREBASE_APP_PREFIX) for n in names)


# -------------------------------------------------------------------------------------------------------- Crashlytics
def crashlytics_wanted(setting, names):
    """"crashlytics" of apkforge.json: true / false win; None or "auto" = the module compiles against firebase-crashlytics"""
    if setting is None or setting == "auto":
        return any(n.startswith(CRASHLYTICS_ARTIFACT) for n in names)
    return bool(setting)


def mapping_id(inputs):
    """The id of one build's R8 mapping: 128 bits of a hash of the build's inputs (anything that changes what R8 sees changes it; the same inputs give the same id on any machine,
    so the build stays reproducible), as 32 hex digits.  The Crashlytics SDK takes it as an opaque string."""
    return hashlib.sha256(util.canon(inputs).encode("utf-8")).hexdigest()[:32]


def mapping_xml(mid):
    """the resource the SDK reads: a string named MAPPING_ID_NAME.  The same file is what `firebase crashlytics:mappingfile:upload --resource-file` reads the id from"""
    return '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <string name="%s" translatable="false">%s</string>\n</resources>\n' % (MAPPING_ID_NAME, mid)


def sidecar_names(stem):
    return stem + "-crashlytics.xml", stem + "-crashlytics.json"


def write_outputs(out_dir, stem, mid, firebase_app_id, application_id, version_name, version_code):
    """Next to the R8 mapping in build/outputs/mapping/: the resource file with the id (what `firebase crashlytics:mappingfile:upload --resource-file` reads) and a small JSON
    record of what the upload needs, so that `apkforge crashlytics-upload` does not have to rebuild or re-derive anything."""
    xml_name, json_name = sidecar_names(stem)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, xml_name), "w", encoding="utf-8", newline="\n") as f:
        f.write(mapping_xml(mid))
    rec = {"mappingId": mid, "mappingFile": stem + "-mapping.txt", "resourceFile": xml_name, "firebaseAppId": firebase_app_id,
           "applicationId": application_id, "versionName": version_name, "versionCode": version_code}
    with open(os.path.join(out_dir, json_name), "w", encoding="utf-8", newline="\n") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
        f.write("\n")


def remove_outputs(out_dir, stem):
    """a build with no mapping to upload must not leave the record of an earlier one looking current"""
    for n in sidecar_names(stem):
        try:
            os.remove(os.path.join(out_dir, n))
        except OSError:
            pass


def read_outputs(out_dir, module=None):
    """-> the record write_outputs() made (with "dir" added) for `module` (":app"), or for the only module that has one"""
    if module:
        found = [os.path.join(out_dir, sidecar_names(module.lstrip(":"))[1])]
        found = [p for p in found if os.path.isfile(p)]
    else:
        found = sorted(glob.glob(os.path.join(out_dir, "*-crashlytics.json")))
    if not found:
        raise ForgeError("nothing to upload: %s has no *-crashlytics.json.  A release build with \"minify\" and Crashlytics on writes it next to the R8 mapping%s"
                         % (out_dir, " (module %s)" % module if module else ""))
    if len(found) > 1:
        raise ForgeError("several modules have a Crashlytics mapping (%s): pick one with --module" % ", ".join(os.path.basename(p)[:-len("-crashlytics.json")] for p in found))
    try:
        with open(found[0], encoding="utf-8") as f:
            rec = json.load(f)
    except ValueError as e:
        raise ForgeError("%s is not valid JSON: %s" % (found[0], e))
    if not isinstance(rec, dict) or not all(isinstance(rec.get(k), str) and rec[k] for k in ("mappingId", "mappingFile", "resourceFile")):
        raise ForgeError("%s is not a record apkforge wrote (an object with mappingId, mappingFile and resourceFile is expected): build again" % os.path.basename(found[0]))
    rec["dir"] = out_dir
    for k in ("mappingFile", "resourceFile"):
        if not os.path.isfile(os.path.join(out_dir, rec.get(k, ""))):
            raise ForgeError("%s names %s, which is not in %s (build again)" % (os.path.basename(found[0]), rec.get(k), out_dir))
    return rec


def upload_command(firebase, rec, app_id):
    """The Firebase CLI's own upload of an R8 / ProGuard mapping: --app is the Firebase app id (1:<number>:android:<hash>), not the package name"""
    return [firebase, "crashlytics:mappingfile:upload", "--app=" + app_id, "--resource-file=" + os.path.join(rec["dir"], rec["resourceFile"]), os.path.join(rec["dir"], rec["mappingFile"])]


def find_firebase(explicit=None):
    if explicit:
        p = explicit if os.path.isfile(explicit) else util.which(explicit)
        if not p:
            raise ForgeError("--firebase %s: no such program" % explicit)
        return p
    p = util.which("firebase")
    if not p:
        raise ForgeError("the Firebase CLI is not installed.  `npm install -g firebase-tools`, then `firebase login` (CI: FIREBASE_TOKEN or GOOGLE_APPLICATION_CREDENTIALS); "
                         "or run the command that --dry-run prints, wherever it is installed")
    return p
