"""AndroidManifest handling: placeholders, namespace vs applicationId, relative class names, and library manifest merging.

Rules follow the Android manifest merger where it matters in practice: the app wins, libraries add components,
permissions, features and queries, attributes missing in the app are filled from libraries, tools:node/remove/replace
are honoured, and a library with a higher minSdkVersion than the app is an error.
"""
import copy
import re
import xml.etree.ElementTree as ET

from .util import ForgeError

ANDROID = "http://schemas.android.com/apk/res/android"
TOOLS = "http://schemas.android.com/tools"
ET.register_namespace("android", ANDROID)
ET.register_namespace("tools", TOOLS)
A, T = "{%s}" % ANDROID, "{%s}" % TOOLS

COMPONENTS = ("activity", "activity-alias", "service", "receiver", "provider")
KEYED = COMPONENTS + ("meta-data", "uses-library", "uses-native-library", "property")


def substitute(text, placeholders):
    def rep(m):
        k = m.group(1)
        return str(placeholders[k]) if k in placeholders else m.group(0)
    return re.sub(r"\$\{([A-Za-z0-9_.]+)\}", rep, text)


def parse(text, where="AndroidManifest.xml"):
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise ForgeError("%s is not valid XML: %s" % (where, e))


def package_of(root):
    return root.get("package")


def _abs_name(name, pkg):
    if not name or not pkg or name.startswith("${"):
        return name
    if name.startswith("."):
        return pkg + name
    if "." not in name:
        return pkg + "." + name
    return name


def expand_names(root, pkg):
    """Relative component names (.Main, Main) refer to the manifest's own package: make them absolute BEFORE merging or renaming."""
    app = root.find("application")
    if app is None:
        return
    for attr in ("name", "backupAgent", "appComponentFactory", "manageSpaceActivity"):
        if app.get(A + attr):
            app.set(A + attr, _abs_name(app.get(A + attr), pkg))
    for el in app:
        if el.tag in COMPONENTS:
            for attr in ("name", "targetActivity", "parentActivityName"):
                if el.get(A + attr):
                    el.set(A + attr, _abs_name(el.get(A + attr), pkg))
    for el in root.findall("instrumentation"):
        if el.get(A + "name"):
            el.set(A + "name", _abs_name(el.get(A + "name"), pkg))


def _key(el):
    t = el.tag
    if t in KEYED or t in ("uses-permission", "uses-permission-sdk-23", "permission", "permission-group"):
        return (t, el.get(A + "name"))
    if t == "uses-feature":
        return (t, el.get(A + "name") or "gl" + str(el.get(A + "glEsVersion")))
    if t == "intent-filter":
        return (t, ET.tostring(el, "unicode"))
    return None


def _tools_node(el):
    return el.get(T + "node")


def _merge_attrs(dst, src):
    for k, v in src.attrib.items():
        if k.startswith("{%s}" % TOOLS):
            continue
        if k not in dst.attrib:
            dst.set(k, v)


def _merge_children(dst, src, warnings, origin):
    have = {}
    for c in dst:
        k = _key(c)
        if k:
            have[k] = c
    removed_all = {c.tag for c in dst if _tools_node(c) == "removeAll"}
    for c in src:
        if c.tag in removed_all:
            continue
        k = _key(c)
        if k is None:
            if c.tag == "queries":
                q = dst.find("queries")
                if q is None:
                    dst.append(copy.deepcopy(c))
                else:
                    _merge_children(q, c, warnings, origin)
            elif c.tag in ("package", "intent", "provider") and dst.tag == "queries":
                if ET.tostring(c, "unicode") not in {ET.tostring(x, "unicode") for x in dst}:
                    dst.append(copy.deepcopy(c))
            continue
        if k in have:
            m = have[k]
            if _tools_node(m) in ("remove", "replace"):
                continue
            _merge_attrs(m, c)
            if c.tag in COMPONENTS:
                _merge_children(m, c, warnings, origin)
        else:
            dst.append(copy.deepcopy(c))
            have[k] = dst[-1]


def merge(main, libs, min_sdk, warnings=None):
    """main: root element; libs: [(root, package, label)] highest priority first. Returns the merged root (main, modified)."""
    warnings = warnings if warnings is not None else []
    override = set((main.find("uses-sdk").get(T + "overrideLibrary") or "").replace(" ", "").split(",")) if main.find("uses-sdk") is not None else set()
    for lroot, lpkg, label in libs:
        expand_names(lroot, lpkg)
        us = lroot.find("uses-sdk")
        if us is not None and us.get(A + "minSdkVersion", "").isdigit() and int(us.get(A + "minSdkVersion")) > min_sdk \
                and lpkg not in override and "*" not in override:
            raise ForgeError("%s declares minSdkVersion %s but the app's minSdk is %d.\n  Raise the app's minSdk, or add "
                             'tools:overrideLibrary="%s" to the app\'s <uses-sdk>.' % (label, us.get(A + "minSdkVersion"), min_sdk, lpkg))
        for c in lroot:
            if c.tag in ("uses-sdk", "supports-screens", "instrumentation", "compatible-screens"):
                continue
            if c.tag == "application":
                app = main.find("application")
                if app is None:
                    app = ET.SubElement(main, "application")
                _merge_attrs(app, c)
                _merge_children(app, c, warnings, label)
            else:
                _merge_children(main, _wrap(c), warnings, label)
    return main


def _wrap(el):
    w = ET.Element("manifest")
    w.append(el)
    return w


def apply_tools(root):
    """Honour tools:node="remove" and drop every tools: attribute / the tools namespace use."""
    def walk(parent):
        for c in list(parent):
            if c.get(T + "node") == "remove":
                parent.remove(c)
                continue
            for k in [k for k in c.attrib if k.startswith("{%s}" % TOOLS)]:
                del c.attrib[k]
            walk(c)
    for k in [k for k in root.attrib if k.startswith("{%s}" % TOOLS)]:
        del root.attrib[k]
    walk(root)


def set_sdk(root, min_sdk, target_sdk):
    us = root.find("uses-sdk")
    if us is None:
        us = ET.Element("uses-sdk")
        root.insert(0, us)
    us.set(A + "minSdkVersion", str(min_sdk))
    us.set(A + "targetSdkVersion", str(target_sdk))


def to_text(root):
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, "unicode") + "\n"


def components(root, pkg=None):
    """Absolute class names of every component declared in a manifest (for the manifest-vs-dex check)."""
    out = []
    app = root.find("application")
    if app is None:
        return out
    if app.get(A + "name"):
        out.append(_abs_name(app.get(A + "name"), pkg))
    for el in app:
        if el.tag in ("activity", "service", "receiver", "provider") and el.get(A + "name"):
            out.append(_abs_name(el.get(A + "name"), pkg))
    return out


# ---------------------------------------------------------------------------------------------------------------- the launcher
MAIN_ACTION = "android.intent.action.MAIN"
LAUNCHER_CATEGORIES = {"android.intent.category.LAUNCHER": "LAUNCHER", "android.intent.category.LEANBACK_LAUNCHER": "LEANBACK_LAUNCHER"}


def _is_disabled(el):
    return (el.get(A + "enabled") or "").strip().lower() == "false"      # an @bool/... reference cannot be resolved here: that counts as enabled


def launchers(root, pkg=None):
    """The activities the system launcher can start, in document order: [{"name", "category", "alias_of"}], one entry per component and category.

    The rules are the platform's, not aapt2's (`aapt2 dump badging` was tried against real APKs and is looser: it lists a disabled activity, merges the
    action of one <intent-filter> with the category of another, and ignores <activity-alias> altogether):
      - the component is an <activity> or an <activity-alias> that is not android:enabled="false"
      - ONE <intent-filter> of it has the action MAIN and the category LAUNCHER (or LEANBACK_LAUNCHER, for TV)
    An alias is listed under its own name, never replaced by its target: that is the component the launcher shows and `am start -n` accepts.
    `root` is a merged manifest (names already absolute) or manifest.parse_xmltree() of a compiled one; pkg makes relative names absolute."""
    app = root.find("application")
    out = []
    if app is None:
        return out
    for el in app:
        if el.tag not in ("activity", "activity-alias") or not el.get(A + "name") or _is_disabled(el):
            continue
        kinds = []
        for f in el.findall("intent-filter"):
            if MAIN_ACTION not in {a.get(A + "name") for a in f.findall("action")}:
                continue
            for c in f.findall("category"):
                k = LAUNCHER_CATEGORIES.get(c.get(A + "name"))
                if k and k not in kinds:
                    kinds.append(k)
        target = _abs_name(el.get(A + "targetActivity"), pkg) if el.tag == "activity-alias" and el.get(A + "targetActivity") else None
        out += [{"name": _abs_name(el.get(A + "name"), pkg), "category": k, "alias_of": target} for k in kinds]
    return out


def pick_launcher(entries):
    """The entry of launchers() that `apkforge run` starts: the first LAUNCHER one in document order, else the first LEANBACK_LAUNCHER one, else None."""
    for kind in ("LAUNCHER", "LEANBACK_LAUNCHER"):
        for entry in entries:
            if entry["category"] == kind:
                return entry
    return None


def launcher_activity(root, pkg=None):
    """The class name `apkforge run` starts (see pick_launcher), or None when the manifest has no launcher activity."""
    entry = pick_launcher(launchers(root, pkg))
    return entry["name"] if entry else None


absolute_name = _abs_name                        # .Main / Main / a.b.Main -> a class name, for the modules that do not own the manifest


_XT_ELEMENT = re.compile(r"^(\s*)E: ([^\s(]+)")
_XT_ATTRIBUTE = re.compile(r"^\s*A: (.*)$")
_XT_RESOURCE_ID = re.compile(r"\(0x[0-9a-fA-F]+\)$")


def parse_xmltree(text):
    """`aapt2 dump xmltree --file AndroidManifest.xml APK` -> an element tree shaped like parse()'s, so launchers() reads the manifest that is inside the APK.
    The dump nests by indentation (E: = element, A: = attribute of the element above it); a string value is quoted and followed by (Raw: ...), a boolean
    is bare (`enabled(0x0101000e)=false`).  Raises ForgeError when the text holds no element."""
    root, stack = None, []                                           # stack: [(indent, element)]
    for line in text.splitlines():
        m = _XT_ELEMENT.match(line)
        if m:
            indent, el = len(m.group(1)), ET.Element(m.group(2))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if stack:
                stack[-1][1].append(el)
            elif root is None:
                root = el
            stack.append((indent, el))
            continue
        m = _XT_ATTRIBUTE.match(line)
        if not m or not stack or "=" not in m.group(1):
            continue
        key, value = m.group(1).split("=", 1)
        cut = value.rfind(" (Raw: ")
        if cut != -1:
            value = value[:cut]
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        ns, _, local = key.rpartition(":")
        local = _XT_RESOURCE_ID.sub("", local)
        stack[-1][1].set("{%s}%s" % (ns, local) if ns else local, value)
    if root is None:
        raise ForgeError("aapt2 printed no manifest element (is this an APK?)")
    return root
