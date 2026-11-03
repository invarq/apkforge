# apkforge

Build Android apps without Gradle, Android Studio or the SDK Manager. apkforge is a Python tool (standard library only, no dependencies) that drives aapt2, javac/kotlinc
and D8/R8 directly, signs APKs and Android App Bundles with its own pure-Python signature engine, and downloads a pinned toolchain the first time it needs one.

**Status: 1.3.0, alpha.** It builds real projects, but it is not a drop-in Gradle replacement: see "What does not work yet" below.

## Install

    pip install apkforge-1.3.0-py3-none-any.whl     # a wheel from a release
    python3 apkforge-1.3.0.pyz --help               # or the single-file build: nothing to install

`pip install apkforge` and `uvx apkforge` work once the package is on PyPI. It needs Python 3.9 or newer and has no Python dependencies.

## Quick start

    apkforge new myapp --package com.acme.myapp     # a project that builds as-is (API 36, minSdk 24)
    apkforge build myapp                            # debug APK, checked after the build -> myapp/build/outputs/
    apkforge run myapp                              # build, install over adb, start it (--logcat follows its log)

    apkforge keygen myapp                           # release keystore and "signing" section (the password is printed once)
    apkforge build myapp --release --aab            # signed APK and an Android App Bundle; what Google Play will object to is printed
    apkforge verify myapp/build/outputs/*.aab       # JSON report on stdout; --play fails on what Google Play refuses
    apkforge test-bundle myapp [--install]          # bundletool's universal APK of the bundle, signed and checked

`apkforge --help` lists the rest, among them `import-gradle` (write `apkforge.json` from an existing Gradle project), `vendor` (offline builds) and `doctor`.

## Java

apkforge needs a Java runtime, because the tools it drives are Java programs: D8/R8, kotlinc and bundletool. It looks for `java` in `JAVA_HOME`, then on `PATH`, then in the
JDK it downloaded itself (`~/.apkforge/toolchain/jdk`). When there is none it downloads a Temurin 21 JDK there, without root, the first time one is needed or when you run
`apkforge doctor`. With `--offline` nothing is downloaded and the error says so. A bare machine, such as a plain Docker image, therefore needs only Python and network access.

## What `doctor` provisions

`apkforge doctor` downloads and checks what a build needs: aapt2, D8/R8, an android.jar (API 35), a JDK when the machine has none, and with `--bundletool` bundletool for
App Bundles. Builds fetch the android.jar of the project's `compileSdk` as well. Everything lives in `~/.apkforge` (set `APKFORGE_HOME` to move it). A download is checked
against the SHA-256 pins in `pins.json` or the checksum its publisher ships, and is otherwise recorded on first use; `--offline` forbids downloads.

## Firebase: `google-services.json` and Crashlytics

Google's Gradle plugin turns `google-services.json` into string resources that the Firebase and Play services SDKs look up by name. apkforge does that step itself, for the APK
and for the App Bundle. Two settings in `apkforge.json` (each `true`, `false` or `"auto"`, the default):

- `"googleServices"`: with `"auto"` it is on when the file is found and the module depends on a `com.google.firebase:` or `com.google.android.gms:` artifact. The file without such a
  dependency is only mentioned; a Firebase dependency without the file is a warning that lists where apkforge looked. `true` without the file is an error.
- `"crashlytics"`: with `"auto"` it is on when the module depends on `firebase-crashlytics`.

The file is searched as Google's plugin searches it, most specific first: `src/<buildType>/google-services.json`, `src/`, then the module directory. The `client` entry used is the one
whose `package_name` is the module's `applicationId`; if none matches, the error lists the ones in the file. A `resValues` entry with the same name wins over the file.

Crashlytics finds the R8 mapping of a crash by one string resource, the mapping id. A release build with `"minify"` gets one: a hash of the build's inputs, so a rebuild of the same
code has the same id, and rotating the signing key does not change it. The build writes `build/outputs/mapping/<module>-mapping.txt`, `<module>-crashlytics.xml` (the resource that
carries the id) and `<module>-crashlytics.json`. `apkforge crashlytics-upload myapp` hands them to `firebase crashlytics:mappingfile:upload`; it needs the Firebase CLI and a login
(`firebase login`, or `FIREBASE_TOKEN` in CI), and apkforge never sees a credential. `--dry-run` prints the command.

The resource rules and the search order come from Google's plugin source, and the Firebase CLI's arguments from its source. Not tried: the upload against the real Crashlytics
backend, and a build against Google's own Firebase artifacts (the tests use stand-in jars).

## What does not work yet

- Hilt: its Gradle plugin rewrites the base class of `@AndroidEntryPoint` classes in bytecode, and apkforge has no such step.
- Product flavors and flavor dimensions.
- RenderScript.
- ECDSA signing keys (RSA only).

`apkforge import-gradle` warns about these when it meets them. Not tested yet: `test-bundle --install` on a real device (only against a stand-in adb), and whether Google Play
accepts a bundle (`docs/design/D2-aab-change-note.md`). apkforge is developed and tested on Linux.

## Development

    python3 -m unittest discover -s tests           # about a minute and a half with the toolchain installed (most of it is the demo builds)

The tests that build demo apps skip themselves until the toolchain is there: run `apkforge doctor --bundletool`, then build any app once (`apkforge new /tmp/demo`,
`apkforge build /tmp/demo`) so that the API 36 android.jar is downloaded.

    python3 tools/build_pyz.py . dist/apkforge-1.3.0.pyz        # the single-file build (carries LICENSE and NOTICE)
    python3 -m pip install build && python3 -m build            # wheel and sdist into dist/
    tools/bare_env.sh setup fresh && tools/bare_env.sh run fresh python3 -m apkforge doctor     # a machine with no Java

The version is written in `apkforge/util.py` (`VERSION`, and `BUILD` for a pre-release label such as `rc1`, empty for a release) and in `pyproject.toml`;
`tests/test_packaging.py` fails when the two differ.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`. Copyright 2026 Gnana Sagar.
