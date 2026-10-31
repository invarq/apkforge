"""Project templates for `apkforge new`. Every template builds as-is with zero downloads beyond the toolchain."""
import os
import re

from .util import ForgeError

MANIFEST_APP = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application
        android:label="@string/app_name"
        android:icon="@drawable/ic_launcher"
        android:allowBackup="true"
        android:theme="@style/AppTheme">
        <activity android:name=".MainActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
    </application>
</manifest>
"""

LAYOUT = """<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent" android:layout_height="match_parent"
    android:orientation="vertical" android:gravity="center" android:padding="24dp">
    <TextView android:id="@+id/title" android:layout_width="wrap_content" android:layout_height="wrap_content"
        android:text="@string/hello" android:textSize="22sp"/>
    <Button android:id="@+id/button" android:layout_width="wrap_content" android:layout_height="wrap_content"
        android:text="@string/tap_me"/>
</LinearLayout>
"""

STRINGS = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="app_name">%(label)s</string>
    <string name="hello">Built without Gradle</string>
    <string name="tap_me">Tap me</string>
</resources>
"""

THEMES = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <style name="AppTheme" parent="@android:style/Theme.Material.Light.DarkActionBar"/>
</resources>
"""

ICON = """<?xml version="1.0" encoding="utf-8"?>
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp" android:height="108dp" android:viewportWidth="108" android:viewportHeight="108">
    <path android:fillColor="#3DDC84" android:pathData="M0,0h108v108h-108z"/>
    <path android:fillColor="#FFFFFF" android:pathData="M30,34h48v8h-48zM30,50h48v8h-48zM30,66h30v8h-30z"/>
</vector>
"""

JAVA_MAIN = """package %(pkg)s;

import android.app.Activity;
import android.os.Bundle;
import android.widget.Button;
import android.widget.TextView;

public class MainActivity extends Activity {
    private int taps;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        final TextView title = findViewById(R.id.title);
        title.setText(getString(R.string.hello) + " (" + BuildConfig.BUILD_TYPE + " " + BuildConfig.VERSION_NAME + ")");
        Button b = findViewById(R.id.button);
        b.setOnClickListener(v -> title.setText("Taps: " + (++taps)));
    }
}
"""

KOTLIN_MAIN = """package %(pkg)s

import android.app.Activity
import android.os.Bundle
import android.widget.Button
import android.widget.TextView

class MainActivity : Activity() {
    private var taps = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        val title = findViewById<TextView>(R.id.title)
        title.text = getString(R.string.hello) + " (" + BuildConfig.BUILD_TYPE + " " + BuildConfig.VERSION_NAME + ")"
        findViewById<Button>(R.id.button).setOnClickListener { title.text = "Taps: ${++taps}" }
    }
}
"""

LIB_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"/>
"""

LIB_JAVA = """package %(pkg)s;

import android.content.Context;

public final class Greeter {
    private Greeter() {}

    public static String greet(Context c) {
        return c.getString(R.string.lib_greeting);
    }
}
"""

LIB_STRINGS = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="lib_greeting">Hello from the library module</string>
</resources>
"""


def _w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


TARGET_SDK = 36            # new projects target the level Google Play asks of new apps and updates (API 36 since 2026-08-31)

_JAVA_WORDS = frozenset("abstract assert boolean break byte case catch char class const continue default do double else enum extends false final finally float "
                        "for goto if implements import instanceof int interface long native new null package private protected public return short static "
                        "strictfp super switch synchronized this throw throws transient true try void volatile while".split())


def _pkg(name):
    p = "".join(c if c.isalnum() else "" for c in name.lower()) or "app"
    return "com.example." + (p if not p[0].isdigit() else "a" + p)


def _check_package(pkg):
    parts = pkg.split(".")
    if len(parts) < 2 or not all(re.fullmatch(r"[a-z][a-z0-9_]*", x) for x in parts) or any(x in _JAVA_WORDS for x in parts):
        raise ForgeError("--package %r is not a usable application id: use two or more lower-case segments, each starting with a letter and without Java "
                         "keywords (for example com.acme.notes)" % pkg)


def _app_files(root, base, pkg, label, kotlin):
    _w(os.path.join(base, "AndroidManifest.xml"), MANIFEST_APP)
    _w(os.path.join(base, "res/layout/activity_main.xml"), LAYOUT)
    _w(os.path.join(base, "res/values/strings.xml"), STRINGS % {"label": label})
    _w(os.path.join(base, "res/values/themes.xml"), THEMES)
    _w(os.path.join(base, "res/drawable/ic_launcher.xml"), ICON)
    src = os.path.join(base, ("kotlin" if kotlin else "java"), *pkg.split("."))
    _w(os.path.join(src, "MainActivity." + ("kt" if kotlin else "java")), (KOTLIN_MAIN if kotlin else JAVA_MAIN) % {"pkg": pkg})


def create(directory, template="java", name=None, package=None):
    if template not in ("java", "kotlin", "multi"):
        raise ForgeError("unknown template %r (java | kotlin | multi)" % template)
    if os.path.exists(directory) and os.listdir(directory):
        raise ForgeError("%s already exists and is not empty" % directory)
    name = name or os.path.basename(os.path.abspath(directory))
    if package:
        _check_package(package)
    pkg = package or _pkg(name)
    label = name.replace("-", " ").replace("_", " ").title()
    root = os.path.abspath(directory)
    if template in ("java", "kotlin"):
        _app_files(root, os.path.join(root, "src/main"), pkg, label, template == "kotlin")
        cfg = {"namespace": pkg, "applicationId": pkg, "minSdk": 24, "targetSdk": TARGET_SDK, "compileSdk": TARGET_SDK, "versionCode": 1,
               "versionName": "1.0", "java": 17}
        files = ["apkforge.json"]
    else:
        lib_pkg = pkg + ".lib"
        _app_files(root, os.path.join(root, "app/src/main"), pkg, label, False)
        _w(os.path.join(root, "lib/src/main/AndroidManifest.xml"), LIB_MANIFEST)
        _w(os.path.join(root, "lib/src/main/res/values/strings.xml"), LIB_STRINGS)
        _w(os.path.join(root, "lib/src/main/java", *lib_pkg.split("."), "Greeter.java"), LIB_JAVA % {"pkg": lib_pkg})
        cfg = {"minSdk": 24, "targetSdk": TARGET_SDK, "compileSdk": TARGET_SDK, "java": 17,
               "modules": {"app": {"type": "app", "namespace": pkg, "applicationId": pkg, "versionCode": 1, "versionName": "1.0",
                                   "dependencies": {"projects": [":lib"]}},
                           "lib": {"type": "library", "namespace": lib_pkg}}}
        files = ["apkforge.json"]
    import json
    _w(os.path.join(root, "apkforge.json"), json.dumps(cfg, indent=2) + "\n")
    _w(os.path.join(root, ".gitignore"), ".apkforge/\nbuild/\n*.p12\n*.jks\n*.keystore\n")      # a signing key must never be committed by accident
    return root, pkg
