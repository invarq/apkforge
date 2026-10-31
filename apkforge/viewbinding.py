"""View binding: the `*Binding` classes that AGP generates from res/layout*/*.xml, without Gradle.

Contract reproduced (what application code can rely on): `<Layout>Binding` in `<namespace>.databinding` implements
androidx.viewbinding.ViewBinding; one public final field per `android:id` (camel-cased); `inflate(LayoutInflater)`,
`inflate(LayoutInflater, ViewGroup, boolean)` (`inflate(LayoutInflater, ViewGroup)` for a <merge> root), `bind(View)` and `getRoot()`.
A view that is not in every configuration of the layout (layout-land, layout-sw600dp ...) is @Nullable, the others are not;
a view whose type differs between configurations becomes a plain View.  <include android:id> becomes the included layout's binding.
`tools:viewBindingIgnore="true"` on the root skips a layout, `tools:viewBindingType` overrides a view's type.

Not reproduced: data binding (<layout> roots are skipped with a warning).

androidx.viewbinding.ViewBinding / ViewBindings live in the library androidx.databinding:viewbinding, which Gradle adds by itself.
SHIM_SOURCES is the same public API (two tiny classes, no annotations); the builder compiles it once into ~/.apkforge/shims/ and puts it
on the classpath unless the project's own dependencies already bring the real library.
"""
import os
import re
import xml.etree.ElementTree as ET

ANDROID = "http://schemas.android.com/apk/res/android"
TOOLS = "http://schemas.android.com/tools"
_A_ID, _A_CLASS, _A_LAYOUT = "{%s}id" % ANDROID, "class", "layout"
_VIEW_PKG = {"View": "android.view", "ViewGroup": "android.view", "TextureView": "android.view", "SurfaceView": "android.view",
             "ViewStub": "android.view", "WebView": "android.webkit"}
_ID = re.compile(r"^@\+?id/([A-Za-z0-9_.]+)$")
_JAVA_WORDS = frozenset("abstract assert boolean break byte case catch char class const continue default do double else enum extends false final finally float "
                        "for goto if implements import instanceof int interface long native new null package private protected public return short static "
                        "strictfp super switch synchronized this throw throws transient true try void volatile while".split())

SHIM_VERSION = "1"
SHIM_SOURCES = {
    "androidx/viewbinding/ViewBinding.java": """package androidx.viewbinding;

import android.view.View;

/** A type which binds the views in a layout XML to fields. */
public interface ViewBinding {
    /** Returns the outermost View in the layout file associated with the Binding. */
    View getRoot();
}
""",
    "androidx/viewbinding/ViewBindings.java": """package androidx.viewbinding;

import android.view.View;
import android.view.ViewGroup;

/** Utilities used by generated view binding classes. */
public class ViewBindings {
    /** Like `findViewById` but skips the view itself, so a binding of a root with an id does not find the root. */
    @SuppressWarnings("unchecked")
    public static <T extends View> T findChildViewById(View rootView, int id) {
        if (!(rootView instanceof ViewGroup)) {
            return null;
        }
        final ViewGroup rootViewGroup = (ViewGroup) rootView;
        final int childCount = rootViewGroup.getChildCount();
        for (int i = 0; i < childCount; i++) {
            final T view = (T) rootViewGroup.getChildAt(i).findViewById(id);
            if (view != null) {
                return view;
            }
        }
        return null;
    }

    private ViewBindings() {
    }
}
""",
}


def camel(name, upper_first):
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", name) if p]
    s = "".join(p[:1].upper() + p[1:] for p in parts)
    if not upper_first:
        s = s[:1].lower() + s[1:]
    if s and s[0].isdigit():
        s = "_" + s
    return s


def binding_class(layout_name):
    return camel(layout_name, True) + "Binding"


def _type(tag, el):
    t = el.get("{%s}viewBindingType" % TOOLS)
    if t:
        return t
    if tag == "view":
        return el.get(_A_CLASS) or "android.view.View"
    if tag in ("fragment",):
        return "android.view.View"
    if "." in tag:
        return tag
    return "%s.%s" % (_VIEW_PKG.get(tag, "android.widget"), tag)


class _Layout:
    """One layout name across all of its configurations (res/layout, res/layout-land, ...)."""

    def __init__(self, name):
        self.name, self.configs = name, 0
        self.root_types, self.root_id, self.merge = set(), None, False
        self.views = {}          # id -> {"types": set, "count": int, "include": layout-name|None}
        self.order = []


def _walk(el, lay, seen, is_root):
    tag = el.tag
    if not isinstance(tag, str):
        return
    raw = el.get(_A_ID)
    m = _ID.match(raw) if raw else None
    if m:
        vid = m.group(1)
        if vid not in seen:
            seen.add(vid)
            inc = None
            if tag == "include":
                lm = re.match(r"^@layout/([A-Za-z0-9_.]+)$", el.get(_A_LAYOUT) or "")
                inc = lm.group(1) if lm else None
            typ = "include:" + inc if inc else _type(tag, el)
            e = lay.views.get(vid)
            if e is None:
                e = lay.views[vid] = {"types": set(), "count": 0, "include": inc}
                lay.order.append(vid)
            e["types"].add(typ)
            e["count"] += 1
            if is_root:
                lay.root_id = vid
    for ch in el:
        _walk(ch, lay, seen, False)


def scan(res_dirs, warn=lambda m: None):
    """{layout name: _Layout} of every layout*/*.xml under the resource directories."""
    out = {}
    for rd in res_dirs:
        try:
            dirs = sorted(d for d in os.listdir(rd) if d == "layout" or d.startswith("layout-"))
        except OSError:
            continue
        for d in dirs:
            for f in sorted(os.listdir(os.path.join(rd, d))):
                if not f.endswith(".xml"):
                    continue
                path = os.path.join(rd, d, f)
                try:
                    root = ET.parse(path).getroot()
                except ET.ParseError as e:
                    raise ValueError("%s: not well-formed XML (%s)" % (path, e))
                name = f[:-4]
                if root.get("{%s}viewBindingIgnore" % TOOLS) == "true":
                    continue
                if root.tag == "layout":
                    warn("%s: <layout> is a data binding layout; data binding is not supported, no binding class is generated" % path)
                    continue
                lay = out.setdefault(name, _Layout(name))
                lay.configs += 1
                if root.tag == "merge":
                    lay.merge = True
                    lay.root_types.add("android.view.View")
                else:
                    lay.root_types.add(_type(root.tag, root))
                _walk(root, lay, set(), True)
    return out


def generate(res_dirs, namespace, out_dir, have_annotations=False, warn=lambda m: None):
    """Write <out_dir>/<ns as path>/databinding/<X>Binding.java for every layout. -> list of generated class names."""
    layouts = scan(res_dirs, warn)
    if not layouts:
        return []
    pkg = namespace + ".databinding"
    nn, nl = ("@androidx.annotation.NonNull ", "@androidx.annotation.Nullable ") if have_annotations else ("", "")
    names = []
    for lname in sorted(layouts):
        lay = layouts[lname]
        cls = binding_class(lname)
        root_t = next(iter(lay.root_types)) if len(lay.root_types) == 1 and not lay.merge else "android.view.View"
        fields, used = [], {"rootView"}          # (field, type, required, id, is_include)
        for vid in lay.order:
            e = lay.views[vid]
            field = camel(vid.split(".")[-1], False)
            if field in _JAVA_WORDS:
                field += "_"
            base, n = field, 1
            while field in used:
                n += 1
                field = "%s%d" % (base, n)
            used.add(field)
            t = next(iter(e["types"])) if len(e["types"]) == 1 else "android.view.View"
            if t.startswith("include:"):
                t = "%s.%s" % (pkg, binding_class(t[8:]))
            fields.append((field, t, e["count"] >= lay.configs, vid, bool(e["include"]) and len(e["types"]) == 1))
        j = []
        w = j.append
        w("// Generated by apkforge (view binding). Do not edit.")
        w("package %s;" % pkg)
        w("")
        w("public final class %s implements androidx.viewbinding.ViewBinding {" % cls)
        w("  %sprivate final %s rootView;" % (nn, root_t))
        w("")
        for f, t, req, _vid, _inc in fields:
            w("  %spublic final %s %s;" % (nn if req else nl, t, f))
            w("")
        params = ["%s%s rootView" % (nn, root_t)] + ["%s%s %s" % (nn if req else nl, t, f) for f, t, req, _v, _i in fields]
        w("  private %s(%s) {" % (cls, ", ".join(params)))
        w("    this.rootView = rootView;")
        for f, _t, _r, _v, _i in fields:
            w("    this.%s = %s;" % (f, f))
        w("  }")
        w("")
        w("  @Override")
        w("  %spublic %s getRoot() {" % (nn, root_t))
        w("    return rootView;")
        w("  }")
        w("")
        if lay.merge:
            w("  %spublic static %s inflate(%sandroid.view.LayoutInflater inflater, %sandroid.view.ViewGroup parent) {" % (nn, cls, nn, nn))
            w("    if (parent == null) {")
            w("      throw new NullPointerException(\"parent\");")
            w("    }")
            w("    inflater.inflate(%s.R.layout.%s, parent);" % (namespace, lname))
            w("    return bind(parent);")
            w("  }")
        else:
            w("  %spublic static %s inflate(%sandroid.view.LayoutInflater inflater) {" % (nn, cls, nn))
            w("    return inflate(inflater, null, false);")
            w("  }")
            w("")
            w("  %spublic static %s inflate(%sandroid.view.LayoutInflater inflater, %sandroid.view.ViewGroup parent, boolean attachToParent) {" % (nn, cls, nn, nl))
            w("    android.view.View root = inflater.inflate(%s.R.layout.%s, parent, false);" % (namespace, lname))
            w("    if (attachToParent) {")
            w("      parent.addView(root);")
            w("    }")
            w("    return bind(root);")
            w("  }")
        w("")
        w("  %spublic static %s bind(%sandroid.view.View rootView) {" % (nn, cls, nn))
        looked_up = [x for x in fields if lay.merge or x[3] != lay.root_id]
        block = any(x[2] for x in looked_up)               # a required view that is missing is an error naming its id
        ind = "      " if block else "    "
        if block:
            w("    int id;")
            w("    missingId:")
            w("    {")
        for f, t, req, vid, inc in fields:
            if vid == lay.root_id and not lay.merge:
                w("%s%s %s = (%s) rootView;" % (ind, t, f, t))
                continue
            rid = "%s.R.id.%s" % (namespace, vid.replace(".", "_"))
            if block:
                w("%sid = %s;" % (ind, rid))
            ref = "id" if block else rid
            if inc:
                w("%sandroid.view.View %s__view = androidx.viewbinding.ViewBindings.findChildViewById(rootView, %s);" % (ind, f, ref))
                w("%s%s %s = %s__view != null ? %s.bind(%s__view) : null;" % (ind, t, f, f, t, f))
            else:
                w("%s%s %s = androidx.viewbinding.ViewBindings.findChildViewById(rootView, %s);" % (ind, t, f, ref))
            if req:
                w("%sif (%s == null) {" % (ind, f))
                w("%s  break missingId;" % ind)
                w("%s}" % ind)
        args = ["rootView" if lay.merge else "(%s) rootView" % root_t] + [f for f, _t, _r, _v, _i in fields]
        w("%sreturn new %s(%s);" % (ind, cls, ", ".join(args)))
        if block:
            w("    }")
            w("    String missingId = rootView.getResources().getResourceName(id);")
            w("    throw new NullPointerException(\"Missing required view with ID: \".concat(missingId));")
        w("  }")
        w("}")
        d = os.path.join(out_dir, *pkg.split("."))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, cls + ".java"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(j) + "\n")
        names.append(cls)
    return names
