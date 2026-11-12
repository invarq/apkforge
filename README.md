# apkforge

**Build Android apps from anywhere Python runs. No Gradle, no Android Studio, no SDK Manager.**

[![PyPI](https://img.shields.io/pypi/v/apkforge.svg)](https://pypi.org/project/apkforge/)
[![Python versions](https://img.shields.io/pypi/pyversions/apkforge.svg)](https://pypi.org/project/apkforge/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/invarq/apkforge/blob/main/LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](https://github.com/invarq/apkforge#status-and-known-limitations)
[![Sponsor](https://img.shields.io/badge/sponsor-%E2%9D%A4-ea4aaa.svg?logo=githubsponsors)](https://github.com/sponsors/invarq)

[Why apkforge](#why-apkforge) · [See it in action](#see-it-in-action) · [Features](#features) · [Commands](#commands) · [Configuration](#configuration) · [Roadmap](#roadmap) · [For companies](#for-companies) · [Sponsor](#support-the-project)

apkforge is a Python tool with **no dependencies** (standard library only). It drives `aapt2`, `javac`/`kotlinc` and D8/R8 directly, signs APKs and Android App Bundles with its own pure-Python signature engine, and downloads a pinned toolchain the first time it needs one. A machine with Python 3.9+ and a network connection can build a signed APK and App Bundle, and see what Google Play will object to before anything is uploaded.

```bash
pip install apkforge
apkforge new myapp --package com.acme.myapp
apkforge build myapp                    # debug APK → myapp/build/outputs/
```

The first run downloads the toolchain into `~/.apkforge`. Once your dependencies are cached or vendored, builds run without a network.

<p align="center">
  <a href="https://github.com/invarq/apkforge/blob/main/docs/assets/apkforge-film-1080p60.mp4">
    <img src="https://raw.githubusercontent.com/invarq/apkforge/main/docs/assets/apkforge-reveal.gif" width="640" alt="The apkforge logo: a spark strikes an anvil-shaped green A, then the apkforge wordmark appears">
  </a>
  <br>
  <sub>Watch the <a href="https://github.com/invarq/apkforge/blob/main/docs/assets/apkforge-film-1080p60.mp4">16-second film</a> (1080p, with sound): the logo, then a build of the starter template, sped up.</sub>
</p>

---

## Why apkforge

**Runs where Gradle is a pain.** A GitHub Action, a plain Docker image, a VPS, a locked-down network where Google's servers are blocked: if Python 3.9+ is there, apkforge can build an Android app. You don't need an Android development environment. Linux is the tested platform (see [Status and known limitations](#status-and-known-limitations)).

**No Python dependency tree.** It uses only the standard library, so there's nothing to install on the Python side, nothing to audit, and nothing to conflict with your environment.

**Catches device crashes at build time.** A reference to a JDK-only API fails the build. A platform API newer than `minSdk` with nothing guarding it is reported. 64-bit native libraries are checked for 16 KB page-size readiness, which Google Play requires for apps that target API 35 or higher.

**Tells you who checked what.** Every verification claim is tagged, so you can see whether apkforge's own code was the only reader (`[internal only]`) or whether an independent tool agreed (`[zipalign]`, `[bundletool]`, `[internal + jarsigner]`).

**A release workflow that's built in.** `keygen`, then `build --release --aab`, then `verify --play`. It signs the APK and the bundle (with a separate upload key if you want one), reads the bundle back, and prints what Google Play will object to. `verify --play` turns those objections into a failing exit status, which is handy for gating a CI/CD pipeline.

**Cached and reproducible.** Each build stage is keyed by the SHA-256 of its inputs, so unchanged inputs are cache hits. The same inputs give a byte-identical APK, even when the project sits in a different directory.

**Kotlin, native code and React Native.** KSP2 and KAPT, the Compose compiler plugin, Kotlin Serialization, Parcelize and view binding. NDK/CMake builds for every ABI. React Native with Hermes bytecode.

**Bring your existing project.** `apkforge import-gradle` writes `apkforge.json` from a Gradle project without running Gradle, and `apkforge vendor` copies every dependency file into `vendor/` for offline and air-gapped builds.

---

## See it in action

These are real outputs from the starter template, with some lines trimmed.

**A cold build, then the same build again.** The second run is all cache hits, and the APK has the same SHA-256.

```text
$ apkforge build myapp
[  0.0s] == :app (debug)
[  2.5s] ok    verified: signature v2 v3 [internal only; apksigner not found]; zip alignment [zipalign]; 1 manifest component in dex [internal only; dexdump not found]; android:exported declared on 1 intent-filter component [internal only]
[  2.5s] ok    APK  build/outputs/com.acme.myapp-debug.apk  (9.6 KB)  sha256 34032971eecdf14a
[  2.5s] actions: 0 cached, 5 built
[  2.5s] done in 2.5s

$ apkforge build myapp
[  0.3s] ok    APK  build/outputs/com.acme.myapp-debug.apk  (9.6 KB)  sha256 34032971eecdf14a
[  0.3s] actions: 5 cached, 0 built
[  0.3s] done in 0.2s
```

**A release build before you have a release key.** apkforge tells you what Google Play would refuse.

```text
$ apkforge build myapp --release --aab
[  2.2s] WARN  release build signed with the DEBUG key: add a "signing" section to apkforge.json before publishing (apkforge keygen does both: the keystore and the section)
[  4.5s] ok    AAB  build/outputs/com.acme.myapp-release.aab  (7.0 KB)  sha256 2fa4c4f297f13472  signed with the DEBUG key
[  4.5s] WARN  Google Play would refuse this bundle: signed with the debug certificate (CN=Android Debug, O=Android, C=US): app stores, Google Play included, do not accept apps signed with it; sign with your upload key (a "uploadSigning" or "signing" section in apkforge.json, and --release)
```

**The same build after `keygen`.** The bundle is signed with your key, read back, and each claim says who confirmed it.

```text
$ apkforge keygen myapp
[  0.8s] ok    created myapp/release.p12 (alias release, RSA 2048, valid about 27 years); certificate sha256 4c72d4894743d680ad459e79696fb72800be586e6fb27d3ca8d258c0ca1f9dcd

$ apkforge build myapp --release --aab
[  2.5s] ok    AAB  build/outputs/com.acme.myapp-release.aab  (6.9 KB)  sha256 ae90b9ab47b76f93  signed with the release key ("signing")
[  2.5s] ok    verified: bundle signature (JAR, SHA-256) [internal + jarsigner]; bundle structure [bundletool]; manifest of com.acme.myapp (versionCode 1, minSdk 24, targetSdk 36) [internal + bundletool]; bundle config: bundletool defaults [internal + bundletool]
```

`apkforge verify myapp/build/outputs/*.aab --play` exits with status 0 for this bundle. Run it on a bundle signed with the debug key and it exits with status 1.

**API guards.** A JDK-only API fails the build, and a platform API that's newer than `minSdk` is reported.

```text
$ apkforge build myapp
ERROR: API guard: 1 reference(s) to APIs that android.jar 36 does not have (they would crash on a device):
    com.acme.myapp.MainActivity: class java.net.http.HttpClient is not in android.jar (a JDK-only API?)
  (set "apiCheck": "warn" in apkforge.json to downgrade this)

$ apkforge build myapp
[  1.3s] WARN  min-API guard: 1 reference(s) to platform APIs newer than minSdk 24, with no SDK_INT check, @RequiresApi or @TargetApi around them (devices below the API throw NoSuchMethodError / NoClassDefFoundError):
    com.acme.myapp.MainActivity.risky: class android.app.NotificationChannel
  (guard each with Build.VERSION.SDK_INT >= N or annotate it, or raise minSdk; "minApiCheck": "off" silences this, "error" fails the build)
```

**Importing a Gradle project.** Every statement is counted, and anything apkforge can't build is reported with its file and line. Passwords in the script are never copied.

```text
$ apkforge import-gradle myproject --dry-run
...
[  0.1s] WARN  NOT MAPPED app/build.gradle.kts:7: id("com.google.dagger.hilt.android") ...
[  0.1s] WARN  NOT MAPPED app/build.gradle.kts:36: flavorDimensions += "tier" ...
[  0.1s] WARN  NOTE app/build.gradle.kts: signing config 'release': passwords are NOT copied ...
[  0.1s] import-gradle: 50 statements read: 29 mapped, 2 ignored on purpose, 6 not mapped, 13 structural or covered by a parent
```

---

## Quick start

```bash
apkforge new myapp --package com.acme.myapp     # a project that builds as-is (API 36, minSdk 24)
apkforge build myapp                            # debug APK, checked after the build → myapp/build/outputs/
apkforge run myapp                              # build, install over adb, start it (--logcat follows its log)

apkforge keygen myapp                           # release keystore + "signing" section in apkforge.json (password printed once)
read -rs APKFORGE_STORE_PASS; export APKFORGE_STORE_PASS
apkforge build myapp --release --aab            # signed APK + App Bundle; what Google Play objects to is printed
apkforge verify myapp/build/outputs/*.aab       # JSON report on stdout; --play makes objections a failing exit status
apkforge test-bundle myapp [--install]          # bundletool's universal APK of the bundle, signed and checked
```

`apkforge --help` lists the rest.

---

## Install

```bash
pip install apkforge                      # from PyPI, Python 3.9+
uvx apkforge --help                       # run without installing, via uv
python3 apkforge-1.3.0.pyz --help         # or the single-file build: nothing to install
```

You can build the single-file `.pyz` yourself with `python3 tools/build_pyz.py . dist/apkforge-1.3.0.pyz` (see [Development](#development)).

---

## Requirements

| What | When you need it |
|---|---|
| Python 3.9+ | Always. Nothing else is installed on the Python side. |
| A network connection | The first run, to download the toolchain, and for dependencies that aren't cached or vendored. `--offline` forbids it. |
| Java | Provided for you. apkforge downloads a Temurin 21 JDK without root when the machine has none. |
| `openssl` | `apkforge keygen`, to create a signing key. If you bring an existing keystore, you don't need it. |
| `adb` | `apkforge run` and `test-bundle --install`. It's found through `--adb`, `PATH`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, or Android Studio's SDK folder. |
| Node.js | React Native builds only, because Metro needs it. |
| `apksigner`, `zipalign`, `dexdump`, `readelf` | Optional. Each one that's installed turns `[internal only]` claims into claims that Google's tool confirmed. `apkforge doctor --allow-apt` installs them with apt-get. |

---

## The origin

This started in a sandboxed environment where Google's servers were blocked. Gradle couldn't download its dependencies, which closed off the usual way to build an Android app. But an APK is just a zip file, a zip file is just data structures, and data structures have published specifications.

So I read the specs: the APK binary format, all four signing schemes, the DEX bytecode layout, the Maven POM format, the aapt2 resource format, and the Gradle module metadata format. Then I reimplemented each transformation from scratch, in Python, with no external libraries. The result is apkforge: the layer that Gradle and the Android Gradle plugin normally provide, rebuilt in Python with no external libraries. It drives Google's own aapt2 and D8/R8 and the Java and Kotlin compilers for the parts that are best left to them.

---

## How it works

apkforge isn't a build script that wraps Gradle. It reimplements the parts of the Android build that Gradle and the Android Gradle plugin normally handle, component by component, from the public specifications.

**Six main components:**

**1. Pure-Python signing engine.** The Android signing specification (the v1 JAR signature, the v2 APK Signature Block, v3, and the v4 `.idsig` file for incremental install) is implemented directly from the PKCS#1, DER encoding, and APK format specs, with RSA PKCS#1 v1.5 and SHA-256. No JVM, no `apksigner`, no signing library. Signing is deterministic: the same APK and key always give the same bytes. Creating a key uses `openssl`, and reading a JKS keystore uses `keytool` from the JDK apkforge downloads.

**2. Maven dependency resolver.** It reads POMs and Gradle module metadata (GMM): parent POMs, properties, BOM imports, `dependencyManagement`, exclusions, and Gradle-style highest-version-wins. GMM is read first when a library publishes it, which is how Kotlin Multiplatform libraries (androidx, Compose, coroutines, okhttp 5, coil 3) point an Android app at the right artifact. Repositories are searched in this order: `vendor/`, `~/.m2`, the `repositories` in `apkforge.json`, then Google Maven and Maven Central. All of it runs in pure Python with no Gradle and no third-party libraries.

**3. Self-provisioning toolchain manager.** `apkforge doctor` downloads and pins aapt2, D8/R8, an `android.jar`, and a Temurin JDK (when the machine has none), all into `~/.apkforge` and without root. The Kotlin compiler, the KSP2 runner, bundletool, and for native builds CMake, Ninja and an NDK are provisioned the same way, either up front with `--kotlin`, `--ksp`, `--bundletool` and `--native`, or when a build first needs them. For every tool, apkforge looks in this order: an environment override, the Android SDK you already have, what it provisioned earlier, then a download. Every download is verified against SHA-256 pins, and `--offline` forbids downloads entirely.

**4. Content-addressed build pipeline.** Each build stage is a pure function of its inputs, keyed by their SHA-256. Unchanged inputs give immediate cache hits, and changed inputs rebuild only the affected stages. Builds are also deterministic: the ZIP writer sorts its entries and stamps every one with a single fixed timestamp (or `SOURCE_DATE_EPOCH`), and signing has no random salt, so identical inputs produce a byte-identical APK.

**5. Independent verification layer.** apkforge reads back what it produces, with readers written from the specs: APK signatures, ZIP alignment, class files and DEX, `resources.arsc`, ELF headers, and the protobuf files inside App Bundles. When Google's `apksigner`, `zipalign`, `dexdump`, `readelf`, `jarsigner` or bundletool are installed, they're asked too, and each claim says who agreed.

**6. Project model and Gradle importer.** `apkforge.json` holds the full project configuration, and it's a readable JSON file rather than a DSL. `apkforge import-gradle` tokenizes and parses existing `build.gradle` files (Groovy and Kotlin DSL) and writes `apkforge.json` without running Gradle, without a JVM, and without treating `build.gradle` as code to execute. Every statement it reads lands in exactly one class: mapped, ignored on purpose, not mapped (reported with its file and line), or structural. The counts are checked against each other.

---

## Features

### Build

- **Java and Kotlin.** `javac` and `kotlinc` (the Kotlin compiler is downloaded on first use), with `java`, `javacArgs`, `kotlinArgs` and `kotlinJvmTarget` settings.
- **Annotation processing.** KSP2, KAPT and plain `annotationProcessor` dependencies, with `kspArgs` and `kaptArgs`. The token `${projectDir}` expands to the module directory, which is how Gradle scripts point Room's schema location.
- **Kotlin compiler plugins.** Jetpack Compose, Kotlin Serialization and Parcelize switch on automatically when the project uses them, and `compose`, `serialization` and `parcelize` force them on or off.
- **View binding.** The `*Binding` classes that the Android Gradle plugin generates from `res/layout*/*.xml`, without Gradle. Data binding isn't supported.
- **Build constants and resources.** `buildConfigFields`, `resValues` and manifest `placeholders`.
- **Manifest merging.** Libraries add components, permissions, features and queries, the app wins on conflicts, `tools:node`, `remove` and `replace` are honored, and a library with a higher `minSdkVersion` than the app is an error.
- **R8.** `"minify": true` runs R8 with default rules condensed from the Android Gradle plugin's `proguard-android-optimize.txt`, and library modules ship their `consumerProguardFiles` inside the AAR.
- **Resource optimization (opt-in).** `aapt2 optimize` shortens resource paths and can use sparse tables and collapsed names. apkforge reads the result back with its own `resources.arsc` reader and refuses it if any resource id, value or kept name was lost.
- **Multi-module projects.** Library modules build to AARs, and `apkforge publish` writes them into a Maven-layout directory.
- **Gradle-like layout, detected for you.** `src/main/java`, `src/main/kotlin`, `res`, `assets` and `jniLibs` are found automatically, flat layouts work too, and `sourceDirs`, `resDirs` and `assetDirs` override them.

### Native code and React Native

- **NDK and CMake.** Point `"native"` at a `CMakeLists.txt` and apkforge builds every ABI in `"abis"`. CMake and Ninja come from PyPI wheels, so every machine uses the same versions. The libraries to package are found through CMake's File API instead of scraping build output, each one is stripped (the unstripped copy stays in the build tree for symbolication), and `libc++_shared.so` is added when the STL needs it. `-ffile-prefix-map` keeps the output independent of where the project and the NDK live, and 64-bit libraries are linked for 16 KB pages when the NDK is older than r28.
- **React Native with Hermes.** Non-debug builds run Metro to bundle the JavaScript and `hermesc` to compile it to Hermes bytecode, with the same commands React Native's own Gradle plugin uses. The bundle goes into `assets/` stored uncompressed, and the source maps are copied to `build/outputs/`. It needs Node.js and your project's `node_modules`.

### Dependencies and offline builds

- **Scopes.** `api`, `implementation`, `compileOnly`, `runtimeOnly`, `annotationProcessor`, `kapt`, `ksp`, `platforms` (BOMs), `projects` and `files`.
- **Vendoring.** `apkforge vendor` copies every dependency file into `vendor/` in Maven layout. Commit it, and the project builds with no network at all.
- **Offline mode.** `--offline` (or `APKFORGE_OFFLINE=1`) forbids every download: toolchain, dependencies and apt.

### Signing and release

- **All four signing schemes, in pure Python.** v1, v2, v3, and the v4 `.idsig` file (`--v4` or `"v4Signing": true`) that lets `adb install` stream incrementally on Android 11 and up.
- **`keygen`.** Creates an RSA 2048 PKCS#12 keystore (valid about 27 years) and the matching `signing` section. The password comes from an environment variable and is never written to the project, an existing keystore is never overwritten, and `--upload` creates a separate upload key.
- **App Bundles.** `--aab` builds the bundle, signs it with `uploadSigning` (or `signing` when there's no upload key), reads it back and checks it. `--no-sign-aab` leaves it unsigned for a CI step that signs somewhere else. `test-bundle` turns the bundle into bundletool's universal APK, signed and checked, and `--install` runs it on a device.
- **What Google Play will object to.** Errors: an unsigned bundle, the debug certificate, and `android:debuggable`. Warnings: a certificate that's valid only until 22 October 2033 or earlier, a `com.example` package, several signatures, and SHA-1-only digests.
- **Keys stay out of git.** The `.gitignore` that `apkforge new` writes excludes `*.p12`, `*.jks` and `*.keystore`.

### Verification and safety nets

- **Tagged claims.** Every claim is tagged `[internal only]`, `[apksigner]`, `[zipalign]`, `[readelf]`, `[internal + jarsigner]`, `[bundletool]`, or `[not checked; why]`, so nothing is vouched for by a tool that didn't look.
- **Manifest and dex agree.** Every component named in the manifest is checked to exist in the dex, and `android:exported` must be declared on components that have intent filters.
- **JDK-only API check.** `javac -cp android.jar` accepts APIs that only the JDK has, such as `java.net.http.HttpClient`, and they then crash on a device. apkforge reads your class files and fails the build (`"apiCheck": "error"` is the default).
- **Min-API guard.** Platform APIs newer than `minSdk` are reported unless an `SDK_INT` comparison, `@RequiresApi`, `@TargetApi` or `@SuppressLint("NewApi")` protects them. The analysis follows the bytecode's control flow, so `&&` and `||` come out right, and where it can't tell, it reports (`"minApiCheck": "warn"` is the default).
- **16 KB page size.** 64-bit native libraries are checked for segment alignment, offset congruence and RELRO placement, plus ZIP alignment, with `readelf` and `zipalign -P 16` as independent second readers when they're installed.
- **Machine-readable results.** `build --json` and `test-bundle --json` print the result as JSON, and `verify` writes a JSON report to stdout with its log on stderr. The exit status is 1 when anything rejects the file.

### Device workflow

- **`apkforge run`.** Builds, installs over adb, starts the launcher activity (or the one you name with `--activity`), and with `--logcat` follows the app's log. The device comes from `--serial`, `$ANDROID_SERIAL`, or the only one connected, and every adb call has a time limit, so a phone that fell asleep ends as an error instead of a hang. `--reinstall` handles a signing-key mismatch (it deletes the app's data, and says so).

### Migration and templates

- **`import-gradle`.** Groovy and Kotlin DSL, with no Gradle, no JVM and no network. Everything it can't map is reported with its file and line, signing passwords are never copied (the `signing` section names an environment variable instead), `--dry-run` prints without writing, and `--force` overwrites an existing `apkforge.json`.
- **Templates.** `apkforge new` creates a `java`, `kotlin` or `multi` (an app plus a library module) project that builds as-is.

### Supply chain and project hygiene

- **Pinned downloads.** SHA-256 pins ship inside the package (`pins.json`), a download that doesn't match its pin is refused unless you pass `--trust-new`, and `apt-get` is never touched without `--allow-apt`.
- **Release pipeline.** CI runs the offline tests on Python 3.9 to 3.14, PyPI publishing uses Trusted Publishing (no token is stored anywhere), the workflow's actions are pinned to commit SHAs, and the wheel and sdist are built with `SOURCE_DATE_EPOCH` set. The workflow also builds the single-file `.pyz` and writes a `SHA256SUMS` file for the release.
- **Zero dependencies, on purpose.** The package declares none, and it's kept that way.

---

## Commands

| Command | What it does |
|---|---|
| `new` | Create a project that builds as-is (`--template` takes `java`, `kotlin` or `multi`; also `--package` and `--name`) |
| `build` | Build an APK, or an AAR for a library module (`--release`, `--aab`, `--no-sign-aab`, `--v4`, `--optimize-resources`, `--react`, `--module`, `--json`) |
| `run` | Build, install on a device or emulator over adb, and start the app (`--serial`, `--logcat`, `--activity`, `--reinstall`) |
| `keygen` | Create a release keystore and the matching `signing` section in `apkforge.json` (`--upload` adds a separate upload key) |
| `verify` | Verify an APK or bundle (signatures, alignment, 16 KB page size) and say which claims an external tool confirmed (`--play`, `--elf`, `--allow-unsigned`) |
| `test-bundle` | Try an App Bundle out locally through bundletool's universal APK, signed and checked (`--install`) |
| `import-gradle` | Write `apkforge.json` from an existing Gradle project, with no Gradle needed (`--dry-run`, `--force`) |
| `vendor` | Copy every dependency file into `vendor/` (Maven layout) so builds can run offline |
| `publish` | Publish a library module into a Maven-layout directory (`--module`, `--repo`, `--group`, `--version-name`) |
| `doctor` | Provision and check the toolchain (`--kotlin`, `--ksp`, `--bundletool`, `--native`, `--allow-apt`) |
| `clean` | Delete `.apkforge/` and `build/` |

The commands that use the toolchain also take `-v` (show cache hits and rebuild reasons), `--offline`, `--allow-apt` and `--trust-new`. `apkforge --help` and `apkforge <command> --help` list every option.

---

## Configuration

Each module is described by `apkforge.json`, a plain JSON file. A single module sits at the project root, and several modules go under a `modules` key. Here's a fuller example:

```json
{
  "namespace": "com.acme.shop",
  "applicationId": "com.acme.shop",
  "minSdk": 24,
  "targetSdk": 36,
  "compileSdk": 36,
  "versionCode": 12,
  "versionName": "2.4.0",
  "java": 17,

  "minify": true,
  "viewBinding": true,
  "optimizeResources": true,
  "abis": ["arm64-v8a", "x86_64"],
  "native": "src/main/cpp/CMakeLists.txt",

  "apiCheck": "error",
  "minApiCheck": "error",
  "elfAlignment": "error",
  "v4Signing": true,

  "buildConfigFields": { "API_URL": "\"https://api.acme.com\"" },
  "placeholders": { "appLabel": "Acme Shop" },

  "signing": { "keystore": "release.p12", "alias": "release", "storePasswordEnv": "APKFORGE_STORE_PASS" },
  "uploadSigning": { "keystore": "upload.p12", "alias": "upload", "storePasswordEnv": "APKFORGE_UPLOAD_PASS" },

  "dependencies": {
    "implementation": ["androidx.core:core-ktx:1.15.0", "androidx.room:room-runtime:2.6.1"],
    "ksp": ["androidx.room:room-compiler:2.6.1"],
    "platforms": ["androidx.compose:compose-bom:2025.01.00"]
  }
}
```

Settings are checked before the build starts. A bad value for `apiCheck`, `minApiCheck` or `elfAlignment`, or a typo inside the `native`, `react`, `optimizeResources` or `uploadSigning` blocks, is an error up front rather than after the compile.

| Setting | Default | What it does |
|---|---|---|
| `minSdk`, `targetSdk`, `compileSdk` | 24, 35, 35 | SDK levels (`apkforge new` writes 36 for the last two). The `android.jar` of `compileSdk` is downloaded on first use |
| `versionCode`, `versionName` | 1, `"1.0"` | The app's version |
| `java` | 17 | Java language level |
| `minify` | false | Run R8 on release builds |
| `optimizeResources` | false | `aapt2 optimize` on release builds. `true`, or an object with `shortenPaths`, `sparseEncoding`, `collapseNames`, `dedupe` and `keep` |
| `viewBinding` | false | Generate the `*Binding` classes |
| `compose`, `serialization`, `parcelize` | automatic | Kotlin compiler plugins. `true` or `false` forces them |
| `native` | false | Build C/C++ with CMake and the NDK. `true`, the path of a `CMakeLists.txt`, or an object with `cmake`, `cmakeVersion`, `ndkVersion`, `arguments`, `cFlags`, `cppFlags`, `targets`, `stl` and `strip` |
| `abis` | all four | `arm64-v8a`, `armeabi-v7a`, `x86_64`, `x86` |
| `react` | false | React Native with Hermes. `true`, or an object with `root`, `entryFile`, `hermes`, `hermesFlags` and more |
| `apiCheck` | `"error"` | References to APIs that `android.jar` doesn't have (JDK-only APIs). `"error"`, `"warn"` or `"off"` |
| `minApiCheck` | `"warn"` | Platform APIs newer than `minSdk` with no guard. `"warn"`, `"error"` or `"off"` |
| `elfAlignment` | `"warn"` | 16 KB page-size check on 64-bit native libraries. `"warn"`, `"error"` or `"off"` |
| `v4Signing` | false | Also write `<apk>.idsig` beside each APK |
| `buildConfigFields`, `resValues`, `placeholders` | empty | Generated constants, resource values and manifest placeholders |
| `kspArgs`, `kaptArgs` | empty | Arguments for KSP and KAPT (`${projectDir}` is the module directory) |
| `javacArgs`, `kotlinArgs`, `kotlinJvmTarget` | empty | Compiler settings |
| `repositories` | empty | Extra Maven repositories |
| `signing`, `uploadSigning` | none | `keystore`, `alias` and `storePasswordEnv` (also `keyPasswordEnv`). `uploadSigning` is the key the App Bundle is signed with |
| `dependencies` | empty | `api`, `implementation`, `compileOnly`, `runtimeOnly`, `annotationProcessor`, `kapt`, `ksp`, `platforms`, `projects` and `files` |
| `modules` | none | Several modules in one file. Each one can set `type` (`app` or `library`) and `dir` |

**Environment variables:**

| Variable | What it does |
|---|---|
| `APKFORGE_HOME` | Where the toolchain and caches live (default `~/.apkforge`) |
| `APKFORGE_OFFLINE=1` | Same as `--offline` |
| `APKFORGE_ALLOW_APT=1` | Same as `--allow-apt` |
| `APKFORGE_STORE_PASS`, `APKFORGE_UPLOAD_PASS`, `APKFORGE_KEY_PASS` | Keystore passwords. `APKFORGE_KEY_PASS` falls back to the store password when it's unset |
| `APKFORGE_AAPT2`, `APKFORGE_ANDROID_JAR`, `APKFORGE_BUNDLETOOL`, `APKFORGE_KSP` | Use your own copy of a tool instead of downloading one |
| `SOURCE_DATE_EPOCH` | The timestamp for every ZIP entry (the default is 1980-01-01, the ZIP format's zero) |

---

## What gets downloaded

`apkforge doctor` downloads and checks what a build needs: aapt2, D8/R8, an `android.jar` (API 35), a Temurin JDK when the machine has none, and, with `--bundletool`, bundletool. Builds also fetch the `android.jar` of the project's `compileSdk`. Add `--kotlin`, `--ksp` or `--native` for the Kotlin compiler, the KSP2 runner, or CMake, Ninja and an NDK (the NDK is a download of several hundred MB from Google). Everything lives in `~/.apkforge` (set `APKFORGE_HOME` to move it). Downloads are checked against SHA-256 pins in `pins.json` or the checksum their publisher ships, and recorded on first use. `--offline` forbids downloads.

Most of the toolchain comes from GitHub releases: aapt2 from `lzhiyong/android-sdk-tools` (a static build), the `android.jar` stubs from `Sable/android-platforms`, and the JDK from Temurin. Libraries come from Google Maven and Maven Central unless you've vendored them.

**Java.** The tools apkforge drives (D8/R8, kotlinc, bundletool) are Java programs, so a Java runtime is required. apkforge looks for `java` in `JAVA_HOME`, then on `PATH`, then in the JDK it downloaded itself. When none is found, it downloads a Temurin 21 JDK into `~/.apkforge/toolchain/jdk` without root.

The build system is Gradle-free and Android-Studio-free, but it isn't JVM-free: the Kotlin compiler and D8/R8 are Java programs. What apkforge removes is the need to install or configure a JVM beforehand.

---

## Status and known limitations

**Version 1.3.0, alpha.** apkforge builds real projects, but it isn't a drop-in replacement for Gradle. Here's what works today and what doesn't:

| Feature | Status |
|---|---|
| Java and Kotlin apps, multi-module projects, library modules (AAR) | ✓ |
| KSP2 and KAPT annotation processing | ✓ |
| Jetpack Compose compiler plugin, Kotlin Serialization, Parcelize | ✓ |
| View binding | ✓ |
| R8 minification and `aapt2 optimize` | ✓ |
| App Bundle (AAB) for Google Play | ✓ |
| CMake / NDK native builds | ✓ |
| React Native + Hermes | ✓ |
| **`google-services.json` and Firebase** | ✗ |
| **Crashlytics build steps** | ✗ |
| **Hilt** | ✗ (its Gradle plugin rewrites `@AndroidEntryPoint` base classes in bytecode, and apkforge has no such step) |
| **Product flavors and flavor dimensions** | ✗ |
| **Build-type source sets beyond debug/release** | ✗ (no `src/debug/`, no `debugImplementation`) |
| Data binding, Navigation Safe Args, AIDL | ✗ |
| Kotlin all-open and no-arg plugins | ✗ |
| ABI and density splits | ✗ |
| `packagingOptions` | ✗ (apkforge's resource merging rules are built in) |
| Dynamic feature modules | ✗ |
| `ndk-build` | ✗ (CMake only) |
| Running unit tests, instrumented tests and lint | ✗ |
| ECDSA signing keys | ✗ (RSA only) |
| v3 key rotation | ✗ (v3 signing works, signing lineage doesn't) |
| RenderScript | ✗ |
| macOS and Windows | Not tested |

`apkforge import-gradle` warns about unsupported features when it encounters them, with the file and line.

What hasn't been tested against the real thing yet: `test-bundle --install` on a real device (only against a stand-in adb), and whether Google Play accepts a bundle signed by apkforge. apkforge is developed and tested on Linux.

---

## Roadmap

The unsupported features above are the roadmap, along with the items below. **Next** is what I'm working toward first, **Planned** is wanted but not scheduled, **Exploring** is under evaluation, and **Not planned** is left out on purpose (open an issue if you need it). There are no dates here on purpose.

**Shipped in 1.3.0**, the first packaged release: `pip install apkforge` and `uvx`, the single-file `.pyz`, Android App Bundles (signed with an optional upload key, read back, checked against what Google Play objects to, and tried out as a universal APK), the Java runtime found or downloaded on a bare machine, and publishing to PyPI through Trusted Publishing.

### Next: Firebase and Crashlytics

- **`google-services.json` processing** for both APK and App Bundle, doing the step Google's Gradle plugin does: turning the file into string resources. A `"googleServices"` setting (`true`, `false` or `"auto"`) controls it.
- **The same lookup order as Google's plugin:** `src/<buildType>/google-services.json`, then `src/`, then the module directory, using the `client` entry whose `package_name` matches the module's `applicationId`.
- **Crashlytics mapping ID generation** for R8 builds, controlled by a `"crashlytics"` setting.
- **`apkforge crashlytics-upload`**, which hands the R8 mapping to the Firebase CLI. It needs the Firebase CLI and a login, and apkforge never sees a credential. `--dry-run` prints the command.
- **Testing the upload against the real Firebase service** before it ships.

### Gradle parity

Everything `import-gradle` reports as not mapped today.

| Item | Notes | Status |
|---|---|---|
| Product flavors and flavor dimensions | The biggest blocker for larger migrations: every flavor-specific setting, source set and dependency is lost today | Planned |
| Build-type source sets and configurations | `src/debug/`, `debugImplementation`, custom build types | Planned |
| Hilt | Needs a bytecode step that rewrites `@AndroidEntryPoint` base classes (hilt-compiler itself already runs on KSP and KAPT) | Planned |
| Navigation Safe Args, Data Binding, AIDL | Code generators apkforge doesn't have yet | Planned |
| Kotlin all-open and no-arg plugins | | Planned |
| ABI and density splits | | Planned |
| `packagingOptions` | Configurable excludes and `pickFirst` | Planned |
| Dynamic feature modules | | Exploring |
| `ndk-build` | CMake is the only native build system today | Exploring |
| Unit and instrumented tests | Tests aren't built by apkforge today | Exploring |

### Signing and release

| Item | Notes | Status |
|---|---|---|
| ECDSA signing keys | RSA only today | Planned |
| APK Signature Scheme v3 key rotation | v3 signing exists, signing lineage doesn't | Planned |
| KMS and HSM signing | Release keys that never touch the build machine | Planned |
| Google Play upload from CI | Through the Play Publishing API | Exploring |

### Supply chain and compliance

| Item | Notes | Status |
|---|---|---|
| SLSA build provenance | Attestations for release builds | Planned |
| CycloneDX SBOM | apkforge already resolves the full dependency graph | Planned |
| Dependency lockfile and Maven checksum verification | | Planned |
| Remote build cache | The action cache is content-addressed already, and this shares it between machines | Planned |
| Audit log | Builds and signing operations | Exploring |

### Developer experience

| Item | Notes | Status |
|---|---|---|
| `apkforge watch` | Rebuild and reinstall on save | Planned |
| VS Code extension | A JSON Schema for `apkforge.json`, with completion and validation | Planned |
| CI output | GitHub Actions annotations and JUnit XML, on top of the JSON output that exists today | Planned |
| Prebuilt container image and GitHub Action | The toolchain baked in for CI | Exploring |

### Platforms

| Item | Notes | Status |
|---|---|---|
| macOS, Windows and Linux arm64 | As tested build hosts. Today apkforge is developed and tested on Linux | Planned |

### Not planned

| Item | Notes | Status |
|---|---|---|
| RenderScript | Deprecated by Google. Open an issue if you depend on it | Not planned |
| Compose Multiplatform | apkforge builds Android apps only | Not planned |

Sponsors and company customers influence the order. If something is blocking you, tell me: [open an issue](https://github.com/invarq/apkforge/issues) or see [For companies](#for-companies) below.

---

## For companies

apkforge is Apache-2.0, so you can use it commercially at no cost.

**Where the commercial offering begins:** Organizations that build release APKs or App Bundles in production have needs beyond what an open-source CLI covers today: signing keys managed in a KMS or HSM, SLSA build provenance, CycloneDX SBOMs, audit logs, and response-time SLAs. These are all on the roadmap. Organizations that fund specific features get them first, plus direct access to the maintainer and a case study arrangement.

**What's available today:**

- **Priority support.** A direct line for build failures and release blockers, with a response time agreed up front.
- **Migration and CI setup.** Moving a Gradle project over with `import-gradle`, setting up offline or air-gapped builds with `vendor`, and configuring a pinned, reproducible toolchain in your CI.
- **Sponsored features.** Fund an item from the roadmap (Hilt, product flavors, ECDSA signing, remote build cache, build attestations, HSM signing integration) and have it built on a timeline you set.
- **Android CI audit.** A one-time review of your existing Android CI/CD pipeline: build times, caching, signing configuration, and reproducibility. It's a fixed fee, and the deliverable is a written report with prioritized recommendations.

If your team builds Android APKs or App Bundles and you're weighing whether apkforge fits your CI/CD pipeline, email [gnana.sagar.research@gmail.com](mailto:gnana.sagar.research@gmail.com) with a line about your project and what you need. I'm happy to do a free 30-minute call with anyone serious about evaluating it.

---

## Support the project

apkforge is free and open source under Apache-2.0, built and maintained independently. If it saves your team time, sponsoring helps keep it maintained and pays for the roadmap above. Stars, bug reports, and telling a colleague help too.

**[Sponsor on GitHub](https://github.com/sponsors/invarq)** · monthly or one-time · card accepted.

### Crypto

Bitcoin (Taproot, on-chain):
```
bc1ps5e479g9swmthd3ly4es6hhdkh2ghc54wdvjxqqcjm0cdx7y25jsnee7ts
```

Ethereum mainnet (ETH and ERC-20 tokens such as USDC and USDT):
```
0x1C4e30151698cf3C4D0f7f1435D675b994968217
```

| Bitcoin (Taproot) | Ethereum |
|:---:|:---:|
| ![QR code for the Bitcoin Taproot address](https://raw.githubusercontent.com/invarq/apkforge/main/docs/assets/donate-btc-taproot.png) | ![QR code for the Ethereum address](https://raw.githubusercontent.com/invarq/apkforge/main/docs/assets/donate-eth.png) |

Check the first and last few characters after you paste, and send only on the network named above.

---

## Contributing

Bug reports, ideas, and pull requests are welcome. For a build problem, include the command you ran and the output of `apkforge doctor`. To work on apkforge itself, see [Development](#development) below.

---

## Development

```bash
python3 -m unittest discover -s tests    # 64 tests, under a minute with the toolchain installed
```

Tests that build demo apps skip themselves until the toolchain is there: run `apkforge doctor --bundletool`, then build any app once so the API 36 `android.jar` is downloaded.

```bash
python3 tools/build_pyz.py . dist/apkforge-1.3.0.pyz   # single-file build (carries LICENSE and NOTICE)
python3 -m pip install build && python3 -m build        # wheel and sdist into dist/
tools/bare_env.sh setup fresh && tools/bare_env.sh run fresh python3 -m apkforge doctor   # test with no Java
```

The version is written in `apkforge/util.py` (`VERSION`, and `BUILD` for a pre-release label) and in `pyproject.toml`; `tests/test_packaging.py` fails when the two differ.

---

## License

Apache License 2.0. See [LICENSE](https://github.com/invarq/apkforge/blob/main/LICENSE) and [NOTICE](https://github.com/invarq/apkforge/blob/main/NOTICE). Copyright 2026 Gnana Sagar.
