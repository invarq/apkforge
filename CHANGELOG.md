# Changelog

## 1.3.1 - 2026-11-04

Adds `google-services.json` and Crashlytics support, and fixes `resValues` missing from App Bundles.

### Added
- **`google-services.json` is processed.** apkforge reads the file the way Google's Gradle plugin does (searched in `src/<buildType>/`, `src/` and the module directory; the `client`
  entry of the module's `applicationId`; a service counts only at status 2) and writes the string resources that the Firebase and Play services SDKs look up by name
  (`google_app_id`, `google_api_key`, `gcm_defaultSenderId`, ..., and `xml/global_tracker.xml`) into the APK and into the bundle. Two new module settings, `"googleServices"`
  and `"crashlytics"`, each `true`, `false` or `"auto"` (the default: on when the file is there and the module depends on a Firebase or Play services artifact, or on
  `firebase-crashlytics`). A `resValues` entry beats the same name from the file. Without the file, a Firebase dependency only produces a warning that says where apkforge looked.
- **A Crashlytics mapping id.** A release build with `"minify"` and Crashlytics on gets the `com.google.firebase.crashlytics.mapping_file_id` resource: a hash of the build's
  inputs (not of the signing key), so a rebuild of the same code has the same id. `build/outputs/mapping/` now holds `<module>-mapping.txt`, `<module>-crashlytics.xml` (the
  resource that carries the id) and `<module>-crashlytics.json` (a small record for the upload).
- **`apkforge crashlytics-upload`** sends that mapping with the Firebase CLI (`firebase crashlytics:mappingfile:upload --app --resource-file`); `--dry-run` prints the command,
  `--app`, `--module` and `--firebase` override what it finds. apkforge never sees a credential: the Firebase CLI holds the login.
- `tests/test_gms.py`: 79 tests for the above, three of them real builds (an APK and a bundle read back with aapt2) against stand-in Firebase jars in a local repository.
- `.github/FUNDING.yml` for the Sponsor button. In the README: how to sponsor, Bitcoin and Ethereum addresses with QR codes, a section for companies, the project film and a
  Firebase section. `docs/assets/` holds the QR codes, the film and the logo files.

### Changed
- `apkforge import-gradle` maps `com.google.gms.google-services` and `com.google.firebase.crashlytics` onto the two new settings instead of reporting them as not supported.
- The README is reorganised (why apkforge, install options, a commands table, status, roadmap, support, contributing). It now lists build-type source sets and dependencies
  (`src/debug/`, `debugImplementation`) among what does not work yet.
- `util.VERSION` is `1.3.1` and `util.BUILD` is empty (a release); `apkforge --version` prints `apkforge 1.3.1`.

### Fixed
- **`resValues` reach the Android App Bundle.** Before, `apkforge build --aab` linked the bundle without the generated `gen_res` folder that `resValues` become, so a resource
  made by `resValues` was in the APK and missing from the bundle that Google Play ships. The APK link and the bundle link now take one overlay list (`Builder.overlay_zips()`),
  so they cannot drift apart again.
- **Generated resources are written as UTF-8.** `values/generated.xml` was written in Python's default encoding, so a non-ASCII `resValues` value stopped the build with a
  `UnicodeEncodeError` wherever that encoding is not UTF-8 (`LC_ALL=C`, some CI images).

## 1.3.0 - 2026-10-31

The first packaged release. It replaces the 1.2.0 dev9 pre-release and includes everything in it.

### Fixed
- **Java is found on a machine that has none on `PATH` or `JAVA_HOME`.** Before, `apkforge build` (and the first `apkforge doctor`) on a bare machine, such as a plain
  Docker image, stopped with "no Java runtime found", even when `doctor` had already downloaded a JDK into `~/.apkforge/toolchain/jdk`: `Toolchain.java()` looked only
  at `JAVA_HOME` and `PATH`. It now also looks in that JDK, and, unless `--offline`, downloads it when there is no Java anywhere (D8 needs Java before any javac step
  has run). An unset `JAVA_HOME` no longer resolves to `bin/java` of the current directory.
- `keytool` (reading a JKS keystore) and `jarsigner` (checking a bundle) are found in the downloaded JDK too: one lookup, `Toolchain.keytool()` and `Toolchain.jarsigner()`.
- **D8 is provisioned atomically.** The carved D8 jar used to be written to its cache path before it had been started. A run that could not start Java left it there, the
  fallback source then overwrote the newer D8 9.x with the older D8 3.3, and every later run trusted the file. The jar is now started first and published only after that.
  *If a machine hit the old failure:* delete `~/.apkforge/toolchain/dexer/r8-d8.jar` and run `apkforge doctor`; `apkforge doctor` prints the D8 version it uses.

### Added
- Everything from the 1.2.0 dev9 pre-release: Android App Bundles that are signed (with an optional separate upload key), read back and checked against what Google Play
  objects to, and tried out locally as a universal APK. See `docs/design/D2-aab-change-note.md`.
- Packaging: `pyproject.toml` (setuptools, no dependencies), the `apkforge` entry point, `pins.json` shipped inside the package. The version is written in
  `apkforge/util.py` and `pyproject.toml`, and `tests/test_packaging.py` fails when they differ.
- Apache License 2.0 (`LICENSE`, with a `NOTICE` file); the single-file `.pyz` carries both.
- A release workflow (`.github/workflows/publish.yml`): the offline tests on Python 3.9 to 3.14, then publishing to PyPI through Trusted Publishing.
- `README.md`, tests for the Java lookup and the D8 cache (`tests/test_toolchain_java.py`), and `tools/bare_env.sh`, a machine with no Java for testing without Docker.

### Changed
- `util.VERSION` is `1.3.0` and `util.BUILD` is empty (a release); `apkforge --version` prints `apkforge 1.3.0`.
