# apkforge

**Build Android apps anywhere Python runs. No Gradle. No Android Studio. No SDK Manager.**

[![PyPI](https://img.shields.io/pypi/v/apkforge.svg)](https://pypi.org/project/apkforge/)
[![Python versions](https://img.shields.io/pypi/pyversions/apkforge.svg)](https://pypi.org/project/apkforge/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/invarq/apkforge/blob/main/LICENSE)
[![Sponsor](https://img.shields.io/badge/sponsor-%E2%9D%A4-ea4aaa.svg?logo=githubsponsors)](https://github.com/sponsors/invarq)

[Why apkforge](#why-apkforge) · [Community vs. Enterprise](#community-vs-enterprise) · [See it in action](#see-it-in-action) · [Features](#features) · [Commands](#commands) · [Configuration](#configuration) · [Enterprise Edition](#enterprise-edition-160--170) · [For companies](#for-companies) · [Sponsor](#support-the-project)

apkforge is a pure-Python Android build system with **zero external Python dependencies**. Standard library only. It compiles resources, Java/Kotlin code, and DEX bytecode, signs APKs and App Bundles with its own deterministic signing engine, and pulls a pinned toolchain automatically on the first run. If a machine has Python 3.9+, it can build, sign, and verify release-ready Android apps and catch Google Play rejections locally before you upload a single byte.

```bash
pip install apkforge
apkforge new myapp --package com.acme.myapp
apkforge build myapp                    # debug APK -> myapp/build/outputs/
```

The first run caches the pinned toolchain in `~/.apkforge`. Once your dependencies are cached or vendored, pull the ethernet cable. Everything builds offline.

<p align="center">
  <a href="https://github.com/invarq/apkforge/blob/main/docs/assets/apkforge-film-1080p60.mp4">
    <img src="https://raw.githubusercontent.com/invarq/apkforge/main/docs/assets/apkforge-reveal.gif" width="640" alt="The apkforge logo: a spark strikes an anvil-shaped green A, then the apkforge wordmark appears">
  </a>
  <br>
  <sub>Watch the <a href="https://github.com/invarq/apkforge/blob/main/docs/assets/apkforge-film-1080p60.mp4">16-second film</a> (1080p, with sound): the logo, then a build of the starter template, sped up.</sub>
</p>

---

## Community vs. Enterprise

apkforge uses a simple open-core split:

* **Community Edition (`1.3.x`, Apache-2.0):** Live on PyPI right now (`pip install apkforge`). Free for personal and commercial use. Everything you need to build, sign, and verify real Android APKs and App Bundles without touching Gradle or Android Studio.
* **Enterprise Engine (`1.6.0`, Commercial License):** Built for heavy CI/CD farms, AI app-builder platforms, ARM64/edge workers, and teams that need full production parity. Replaces `aapt2` and `d8` with pure-Python compilers that match Google's outputs byte-for-byte, and adds Hilt, Firebase, product flavors, and standalone test runners.
* **Enterprise `1.7.0` (Dropping Next):** Ships the Zero-JDK Pure-Python `javac` compiler. No JVM left anywhere in the Java build pipeline.

| Capability | Community (`1.3.x`, Apache-2.0) | Enterprise (`1.6.0+`, Commercial) |
| :--- | :---: | :---: |
| **Pure-Python APK & AAB Packaging & v1-v4 Signing** | ✓ | ✓ |
| **Java, Kotlin, Compose, KSP2, KAPT & View Binding** | ✓ | ✓ |
| **CMake/NDK Native Builds & React Native (Hermes)** | ✓ | ✓ |
| **Hermetic `SOURCE_DATE_EPOCH` Builds & Content-Addressed Caching** | ✓ | ✓ |
| **Control-Flow Bytecode API Guards & 16 KB ELF Page-Size Auditing** | ✓ | ✓ |
| **Machine-Readable CI/CD JSON Output (`--json`) & Offline Vendoring** | ✓ | ✓ |
| **Gradle Project Importer (`import-gradle`)** | ✓ | ✓ |
| **Byte-for-Byte Deterministic `aapt2` & `d8` Compilers (Pure Python)** | - | **1.6.0** |
| **Zero-SDK ARM64 / On-Device Compilation (Termux, Raspberry Pi, Edge)** | - | **1.6.0** |
| **Dual-Engine Runtime Switching (Pure-Python vs. Host Toolchain)** | - | **1.6.0** |
| **Built-In Mock `android.jar` Generator & Standalone Test Runners** | - | **1.6.0** |
| **Full Multi-Module Gradle Parity (Product Flavors & Build Types)** | - | **1.6.0** |
| **Hilt Dependency Injection, Firebase & Crashlytics Integration** | - | **1.6.0** |
| **Zero-JDK Pure-Python `javac` Compiler (No JVM Required)** | - | **1.7.0 (Next)** |
| **KMS/HSM Signing, SLSA Provenance, CycloneDX SBOM & Audit Logs** | - | **Enterprise** |

Want to license `1.6.0` or get early access to `1.7.0`? Jump to [For companies](#for-companies).

---

## Why apkforge

**Runs anywhere Python does.** A bare Docker container, a GitHub Action, a $5 VPS, an ARM64 edge box, or a locked-down network where Google's servers are unreachable. No Gradle daemon eating 2 GB of RAM. No Android Studio install.

**Zero Python dependencies.** Just the standard library. Nothing to `pip` conflict, nothing to audit on the Python side, nothing to break your environment six months from now.

**Byte-identical, reproducible builds.** Every build stage is hashed by the SHA-256 of its inputs. Nothing changed? Instant cache hit. `SOURCE_DATE_EPOCH` locks all ZIP timestamps (defaulting to ZIP epoch zero `1980-01-01`), ZIP entries are sorted canonically, native C/C++ builds strip host paths via `-ffile-prefix-map`, and signing uses no random salt. Build the same project on two different machines in two different folders and you get the **exact same APK down to the final SHA-256**.

**Catches device crashes before the APK ever leaves your machine.** Call a JDK-only API that Android doesn't ship? Build fails. Call a platform API newer than `minSdk` without checking `Build.VERSION.SDK_INT`? Bytecode control-flow analysis (handling compound `&&` and `||` branches) catches it. Ship a 64-bit native `.so` that violates Google Play's 16 KB page-size rule? The ELF auditor flags it right at the binary header level.

**Built for CI scripts and AI agents.** Gradle dumps walls of unstructured console noise. `apkforge build --json`, `test-bundle --json`, and `verify` print clean JSON reports with strict exit codes (`0` for Play-ready, `1` for blockers).

**Receipts on every check.** Every verification line tags who actually checked it: apkforge's own binary readers (`[internal only]`) or Google's external tools agreeing (`[zipalign]`, `[bundletool]`, `[internal + jarsigner]`). No silent assumptions.

**Release workflow out of the box.** `keygen`, then `build --release --aab`, then `verify --play`. It signs your APK and App Bundle (supporting a separate upload key), reads the finished bundle back, and tells you straight up if Google Play is going to reject it.

**Real Android features, not toy demos.** Kotlin, KSP2, KAPT, Jetpack Compose, Kotlin Serialization, Parcelize, View Binding, CMake/NDK across every ABI, and React Native with Hermes bytecode.

**Bring existing Gradle projects.** `apkforge import-gradle` parses your `build.gradle` / `build.gradle.kts` files statically without ever running Gradle or a JVM, and `apkforge vendor` pulls every dependency into `vendor/` so air-gapped builds just work.

---

## See it in action

Real terminal runs from the starter template (trimmed slightly for space).

**Cold build, then an instant cached rebuild.** Notice the SHA-256 doesn't budge a single bit.

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

**Release build before setting up a release key.** apkforge flags why Play will reject it before you waste time uploading.

```text
$ apkforge build myapp --release --aab
[  2.2s] WARN  release build signed with the DEBUG key: add a "signing" section to apkforge.json before publishing (apkforge keygen does both: the keystore and the section)
[  4.5s] ok    AAB  build/outputs/com.acme.myapp-release.aab  (7.0 KB)  sha256 2fa4c4f297f13472  signed with the DEBUG key
[  4.5s] WARN  Google Play would refuse this bundle: signed with the debug certificate (CN=Android Debug, O=Android, C=US): app stores, Google Play included, do not accept apps signed with it; sign with your upload key (a "uploadSigning" or "signing" section in apkforge.json, and --release)
```

**Now run `keygen` and build again.** Signed with your key, read back, and double-checked.

```text
$ apkforge keygen myapp
[  0.8s] ok    created myapp/release.p12 (alias release, RSA 2048, valid about 27 years); certificate sha256 4c72d4894743d680ad459e79696fb72800be586e6fb27d3ca8d258c0ca1f9dcd

$ apkforge build myapp --release --aab
[  2.5s] ok    AAB  build/outputs/com.acme.myapp-release.aab  (6.9 KB)  sha256 ae90b9ab47b76f93  signed with the release key ("signing")
[  2.5s] ok    verified: bundle signature (JAR, SHA-256) [internal + jarsigner]; bundle structure [bundletool]; manifest of com.acme.myapp (versionCode 1, minSdk 24, targetSdk 36) [internal + bundletool]; bundle config: bundletool defaults [internal + bundletool]
```

Run `apkforge verify myapp/build/outputs/*.aab --play` and it exits `0`. Pass it a debug-signed bundle and it exits `1`.

**API guards in action.** Catching a JDK-only crasher and an unguarded newer platform API.

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

**Importing a Gradle project.** Every single statement gets counted. Anything unmapped prints its exact file and line. Passwords are never copied.

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
apkforge new myapp --package com.acme.myapp     # builds as-is (API 36, minSdk 24)
apkforge build myapp                            # debug APK, verified -> myapp/build/outputs/
apkforge run myapp                              # build, adb install, launch (--logcat streams logs)

apkforge keygen myapp                           # release keystore + "signing" config (password printed once)
read -rs APKFORGE_STORE_PASS; export APKFORGE_STORE_PASS
apkforge build myapp --release --aab            # signed APK + App Bundle + local Play checks
apkforge verify myapp/build/outputs/*.aab       # JSON report on stdout; --play fails exit code on blockers
apkforge test-bundle myapp [--install]          # universal APK via bundletool, signed and checked
```

`apkforge --help` prints everything else.

---

## Install

```bash
pip install apkforge                      # from PyPI, Python 3.9+
uvx apkforge --help                       # zero-install run via uv
python3 apkforge-1.3.0.pyz --help         # single-file zipapp: drop it anywhere and run
```

Want to pack the `.pyz` yourself? Run `python3 tools/build_pyz.py . dist/apkforge-1.3.0.pyz` (see [Development](#development)).

---

## Requirements

| What | When you need it |
|---|---|
| Python 3.9+ | Always. Zero third-party Python packages are installed. |
| Network connection | First run only, to pull the toolchain and uncached deps. `--offline` kills all network calls. |
| Java | Auto-provisioned in Community (`1.3.x`) via Temurin 21 JDK without root if missing. Gone completely in Enterprise `1.7.0`. |
| `openssl` | Only for `apkforge keygen`. Skip it if you already have a keystore. |
| `adb` | Only for `apkforge run` and `test-bundle --install`. Found via `--adb`, `PATH`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, or Android Studio's SDK path. |
| Node.js | React Native builds only (Metro needs it). |
| `apksigner`, `zipalign`, `dexdump`, `readelf` | Optional cross-checkers. When present, `[internal only]` claims upgrade to dual-verified claims. `apkforge doctor --allow-apt` installs them via `apt-get`. |

---

## The origin

A few months ago I was trying to build a simple Android app with claude, Claude doesn't have Gradle. The sandbox doesn't have a JVM in the right state for it. And it doesn't have Android Studio. It could write every line of Kotlin I needed and it had, over several interactions but it couldn't turn that Kotlin into an APK. So that got me thinking. An APK is literally just a ZIP file full of documented binary blobs. Python can pack ZIP files. Python can write bytes. So why do we need a giant JVM daemon just to stitch them together?

So I pulled the published Android specs and AOSP sources and started breaking the build chain down piece by piece: the APK binary layout, all four signing schemes, DEX bytecode, `resources.arsc`, Maven POMs, and Gradle Module Metadata. I wired up an automated verification loop against Google's own binaries and kept grinding until the outputs matched.

What started as a stubborn workaround turned into **Community Edition (`1.3.x`)**, which became an AGP alternative. Then I kept pushing. In **Enterprise (`1.6.0`)**, `aapt2` and `d8` themselves are pure Python, matching Google's official compilers **byte-for-byte**. Next up in **`1.7.0`**, I'm working on `javac` with the same level of rigor.

---

## How it works

apkforge does not wrap Gradle. It replaces the Android build layer piece by piece from the public specs.

**Six core pieces:**

**1. Pure-Python signing engine.** All four Android signing schemes (v1 JAR, v2 APK Signature Block, v3, and v4 `.idsig` for incremental streaming installs) are built straight from the PKCS#1, DER, and APK specs using RSA PKCS#1 v1.5 and SHA-256. No JVM, no `apksigner`, no crypto packages. Strictly deterministic: same APK and same key always produce identical bytes. Key creation uses `openssl`, and reading legacy JKS files uses `keytool`.

**2. Maven & Gradle Module Metadata resolver.** Parses POMs and Gradle Module Metadata (GMM): parent POMs, properties, BOMs, `dependencyManagement`, exclusions, and Gradle-style highest-version-wins. GMM takes priority whenever a library ships it, so Kotlin Multiplatform libraries (`androidx`, Compose, Coroutines, OkHttp 5, Coil 3) land on the right Android artifact every time. Searches in strict order: `vendor/`, `~/.m2`, custom `repositories` in `apkforge.json`, Google Maven, then Maven Central.

**3. Self-provisioning toolchain manager.** `apkforge doctor` downloads and pins `aapt2`, `D8/R8`, `android.jar`, and a Temurin 21 JDK (if your machine has no Java) straight into `~/.apkforge`, no root needed. `kotlinc`, KSP2, `bundletool`, and CMake/Ninja/NDK download the same way on demand or upfront (`--kotlin`, `--ksp`, `--bundletool`, `--native`). Checks env overrides first, then any existing Android SDK, then cached tools, then pinned downloads checked against SHA-256 hashes. `--offline` shuts down all network access.

**4. Content-addressed build cache.** Every build stage is keyed by the SHA-256 of its inputs. Unchanged inputs skip straight past work; changed inputs rebuild only what depends on them. Sorted ZIP entries, fixed timestamps (`SOURCE_DATE_EPOCH`, defaulting to `1980-01-01`), and salt-free signing keep outputs byte-identical across machines and folders.

**5. Independent binary verification.** apkforge reads back everything it writes using its own binary parsers: APK signature blocks, ZIP alignment, `.class` and `.dex` bytecode, `resources.arsc`, ELF headers, and App Bundle protobufs. If Google's `apksigner`, `zipalign`, `dexdump`, `readelf`, `jarsigner`, or `bundletool` are on the machine, apkforge asks them too and tags every claim with who signed off.

**6. Static Gradle importer & plain JSON config.** `apkforge.json` replaces Groovy/Kotlin build scripts with readable JSON. `apkforge import-gradle` tokenizes and parses `build.gradle` and `build.gradle.kts` statically. No Gradle, no JVM, no executing random build scripts. Every statement read lands in one bucket (mapped, ignored on purpose, unmapped with exact file and line, or structural), and the totals cross-check.

---

## Features

### Build

- **Java and Kotlin.** Runs `javac` and `kotlinc` (auto-downloaded on first use), with `java`, `javacArgs`, `kotlinArgs`, and `kotlinJvmTarget` controls.
- **Annotation processing.** KSP2, KAPT, and classic `annotationProcessor`, with `kspArgs` and `kaptArgs`. `${projectDir}` expands to the module folder automatically (handy for Room schema exports).
- **Kotlin compiler plugins.** Jetpack Compose, Kotlin Serialization, and Parcelize turn on automatically when detected in dependencies, or you can force them via `compose`, `serialization`, and `parcelize`.
- **View binding.** Generates `*Binding` classes straight from `res/layout*/*.xml` with zero Gradle.
- **Build constants and resources.** `buildConfigFields`, `resValues`, and manifest `placeholders`.
- **Manifest merging.** Merges library components, permissions, features, and `<queries>` with app-wins resolution, honors `tools:node`, `remove`, and `replace`, and errors out if a library demands a higher `minSdkVersion` than your app.
- **R8 shrinking.** `"minify": true` runs R8 with optimized defaults pulled from `proguard-android-optimize.txt`, and library modules pack their `consumerProguardFiles` right into the AAR.
- **Resource optimization (opt-in).** Runs `aapt2 optimize` for shorter resource paths, sparse tables, and collapsed names, then reads the output back through apkforge's own `resources.arsc` parser and rejects it if a single resource ID, value, or kept name went missing.
- **Multi-module workspaces.** Library modules output AARs, and `apkforge publish` writes them straight into a local Maven-layout repo.
- **Zero-boilerplate layout detection.** Picks up standard Gradle folders (`src/main/java`, `src/main/kotlin`, `res`, `assets`, `jniLibs`) as well as flat layouts automatically, with `sourceDirs`, `resDirs`, and `assetDirs` overrides.

### Native code and React Native

- **NDK and CMake.** Point `"native"` at a `CMakeLists.txt` and apkforge builds every ABI in `"abis"`. CMake and Ninja come from pinned PyPI wheels so every machine builds identically. Output `.so` files come from CMake's File API (no fragile stdout scraping), get stripped automatically (keeping unstripped copies in the build tree for symbolication), and pull in `libc++_shared.so` whenever the STL needs it. `-ffile-prefix-map` keeps paths reproducible, and 64-bit libs automatically get 16 KB page-size linker flags on NDKs older than r28.
- **React Native with Hermes.** Non-debug builds invoke Metro to bundle JS and `hermesc` to compile Hermes bytecode with the exact flags React Native's Gradle plugin uses. The bundle lands uncompressed in `assets/` and source maps copy to `build/outputs/`. Needs Node.js and `node_modules`.

### Dependencies and offline builds

- **Scopes.** `api`, `implementation`, `compileOnly`, `runtimeOnly`, `annotationProcessor`, `kapt`, `ksp`, `platforms` (BOMs), `projects`, and `files`.
- **Vendoring.** `apkforge vendor` mirrors every resolved dependency into `vendor/` in standard Maven layout. Commit that folder and build air-gapped forever.
- **Offline mode.** `--offline` (or `APKFORGE_OFFLINE=1`) blocks every download: toolchains, Maven libraries, and apt.

### Signing and release

- **All four signing schemes in pure Python.** v1, v2, v3, and v4 `.idsig` (`--v4` or `"v4Signing": true`) for incremental `adb` streaming installs on Android 11+.
- **`keygen`.** Generates an RSA 2048 PKCS#12 keystore (valid ~27 years) and wires up the `signing` block in `apkforge.json`. Passwords live only in env vars, never in project files. Existing keystores are never clobbered, and `--upload` generates a separate Google Play upload key.
- **App Bundles.** `--aab` builds the bundle, signs with `uploadSigning` (or `signing`), reads it back, and verifies everything. `--no-sign-aab` leaves it unsigned for a separate CI signing step. `test-bundle` builds bundletool's universal APK, signs and checks it, and `--install` pushes it straight to a phone.
- **Google Play pre-flight checks.** Hard errors: unsigned bundle, debug cert, `android:debuggable`. Warnings: certs expiring on or before 22 October 2033, `com.example` package names, multiple signatures, and SHA-1-only digests.
- **Keys stay out of git.** `apkforge new` writes a `.gitignore` excluding `*.p12`, `*.jks`, and `*.keystore` right from the start.

### Verification and safety nets

- **Tagged claims.** Every check prints `[internal only]`, `[apksigner]`, `[zipalign]`, `[readelf]`, `[internal + jarsigner]`, `[bundletool]`, or `[not checked; why]`. You always know what actually inspected the file.
- **Manifest vs. DEX check.** Confirms every component named in `AndroidManifest.xml` actually exists in DEX, and enforces `android:exported` on anything with an intent filter.
- **JDK-only API guard.** `javac -cp android.jar` happily compiles calls like `java.net.http.HttpClient` that explode at runtime on a real phone. apkforge scans compiled classes and kills the build (`"apiCheck": "error"` by default).
- **Control-flow Min-API guard.** Flags platform APIs newer than `minSdk` unless wrapped in an `SDK_INT` check, `@RequiresApi`, `@TargetApi`, or `@SuppressLint("NewApi")`. Walks real bytecode control flow so `&&` and `||` checks work properly (`"minApiCheck": "warn"` by default).
- **16 KB page-size ELF check.** Audits 64-bit native `.so` libraries for ELF segment alignment, offset congruence, RELRO placement, and ZIP alignment, cross-checking with `readelf` and `zipalign -P 16` when installed.
- **Machine-readable output.** `build --json` and `test-bundle --json` emit structured JSON. `verify` writes its JSON audit report to `stdout` (logs go to `stderr`) and exits `1` on any rejection.

### Device workflow

- **`apkforge run`.** Builds, installs via `adb`, launches the launcher activity (or `--activity`), and streams logs with `--logcat`. Picks `--serial`, `$ANDROID_SERIAL`, or the sole plugged-in device. Every `adb` call has a strict timeout so a sleeping phone fails fast instead of hanging your shell. `--reinstall` handles signing-key mismatches cleanly (warning you before wiping app data).

### Migration and templates

- **`import-gradle`.** Parses Groovy and Kotlin DSL statically with zero Gradle, JVM, or network. Reports anything unmapped with file and line, never copies signing passwords (maps them to env vars instead), supports `--dry-run`, and won't overwrite `apkforge.json` without `--force`.
- **Templates.** `apkforge new` spins up `java`, `kotlin`, or `multi` (app + library) starter projects that build immediately.

### Supply chain and project hygiene

- **Pinned downloads.** SHA-256 pins ship inside `pins.json`. Hash mismatches are refused unless you explicitly pass `--trust-new`, and `apt-get` is never touched without `--allow-apt`.
- **Locked-down release pipeline.** CI runs offline tests across Python 3.9 to 3.14, PyPI publishes via Trusted Publishing (zero stored tokens), GitHub Actions are pinned to commit SHAs, wheels and sdists build with `SOURCE_DATE_EPOCH`, and every release ships a single-file `.pyz` with `SHA256SUMS`.
- **Zero dependencies by rule.** Declared empty in packaging, kept empty on purpose.

---

## Commands

| Command | What it does |
|---|---|
| `new` | Scaffold a ready-to-build project (`--template`: `java`, `kotlin`, or `multi`; plus `--package` and `--name`) |
| `build` | Build an APK, or an AAR for a library module (`--release`, `--aab`, `--no-sign-aab`, `--v4`, `--optimize-resources`, `--react`, `--module`, `--json`) |
| `run` | Build, install over `adb`, and launch (`--serial`, `--logcat`, `--activity`, `--reinstall`) |
| `keygen` | Create a release keystore and matching `signing` block in `apkforge.json` (`--upload` adds a separate upload key) |
| `verify` | Audit an APK or App Bundle (signatures, alignment, 16 KB pages, Play rules) with tagged tool claims (`--play`, `--elf`, `--allow-unsigned`) |
| `test-bundle` | Build, sign, and check bundletool's universal APK from a bundle (`--install`) |
| `import-gradle` | Convert an existing Gradle project to `apkforge.json` statically (`--dry-run`, `--force`) |
| `vendor` | Copy every dependency into `vendor/` (Maven layout) for air-gapped offline builds |
| `publish` | Publish a library module to a local Maven-layout directory (`--module`, `--repo`, `--group`, `--version-name`) |
| `doctor` | Provision and check toolchain pieces (`--kotlin`, `--ksp`, `--bundletool`, `--native`, `--allow-apt`) |
| `clean` | Wipe `.apkforge/` caches and `build/` outputs |

Toolchain commands also take `-v` (cache hits and rebuild reasons), `--offline`, `--allow-apt`, and `--trust-new`. Run `apkforge --help` or `apkforge <command> --help` for every flag.

---

## Configuration

Every module lives in `apkforge.json`. Single-module projects put it at the root; multi-module setups group modules under `modules`:

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

Config values are validated before a single file compiles. A bad value in `apiCheck`, `minApiCheck`, or `elfAlignment`, or a typo inside `native`, `react`, `optimizeResources`, or `uploadSigning` fails immediately upfront.

| Setting | Default | What it does |
|---|---|---|
| `minSdk`, `targetSdk`, `compileSdk` | 24, 35, 35 | SDK levels (`apkforge new` writes 36 for target/compile). Downloads the matching `android.jar` automatically |
| `versionCode`, `versionName` | 1, `"1.0"` | App version metadata |
| `java` | 17 | Java language level |
| `minify` | false | Run R8 on release builds |
| `optimizeResources` | false | Run `aapt2 optimize` on release builds. `true`, or an object with `shortenPaths`, `sparseEncoding`, `collapseNames`, `dedupe`, and `keep` |
| `viewBinding` | false | Generate `*Binding` classes from layout XMLs |
| `compose`, `serialization`, `parcelize` | automatic | Kotlin compiler plugins (`true` or `false` forces them) |
| `native` | false | Build C/C++ via CMake and NDK. `true`, path to `CMakeLists.txt`, or an object with `cmake`, `cmakeVersion`, `ndkVersion`, `arguments`, `cFlags`, `cppFlags`, `targets`, `stl`, and `strip` |
| `abis` | all four | `arm64-v8a`, `armeabi-v7a`, `x86_64`, `x86` |
| `react` | false | React Native with Hermes. `true`, or an object with `root`, `entryFile`, `hermes`, `hermesFlags`, and related options |
| `apiCheck` | `"error"` | Catch JDK-only APIs absent from `android.jar` (`"error"`, `"warn"`, or `"off"`) |
| `minApiCheck` | `"warn"` | Flag unguarded platform APIs newer than `minSdk` (`"warn"`, `"error"`, or `"off"`) |
| `elfAlignment` | `"warn"` | 16 KB page-size check on 64-bit native `.so` libs (`"warn"`, `"error"`, or `"off"`) |
| `v4Signing` | false | Write `<apk>.idsig` next to each APK |
| `buildConfigFields`, `resValues`, `placeholders` | empty | Generated constants, XML resource values, and manifest placeholders |
| `kspArgs`, `kaptArgs` | empty | Arguments for KSP and KAPT (`${projectDir}` expands to the module folder) |
| `javacArgs`, `kotlinArgs`, `kotlinJvmTarget` | empty | Compiler flags |
| `repositories` | empty | Extra Maven repositories |
| `signing`, `uploadSigning` | none | `keystore`, `alias`, and `storePasswordEnv` (plus optional `keyPasswordEnv`). `uploadSigning` signs the App Bundle |
| `dependencies` | empty | `api`, `implementation`, `compileOnly`, `runtimeOnly`, `annotationProcessor`, `kapt`, `ksp`, `platforms`, `projects`, and `files` |
| `modules` | none | Multi-module map in one file (`type`: `app` or `library`, and `dir`) |

**Environment variables:**

| Variable | What it does |
|---|---|
| `APKFORGE_HOME` | Toolchain and cache folder (default `~/.apkforge`) |
| `APKFORGE_OFFLINE=1` | Same as `--offline` |
| `APKFORGE_ALLOW_APT=1` | Same as `--allow-apt` |
| `APKFORGE_STORE_PASS`, `APKFORGE_UPLOAD_PASS`, `APKFORGE_KEY_PASS` | Keystore passwords (`APKFORGE_KEY_PASS` falls back to store password if unset) |
| `APKFORGE_AAPT2`, `APKFORGE_ANDROID_JAR`, `APKFORGE_BUNDLETOOL`, `APKFORGE_KSP` | Use your own local tool copies instead of downloading |
| `SOURCE_DATE_EPOCH` | Fixed timestamp written to every ZIP entry (default `1980-01-01`, ZIP epoch zero) |

---

## What gets downloaded (Community Edition `1.3.x`)

In Community Edition (`1.3.x`), `apkforge doctor` pulls and verifies what the build needs: `aapt2`, `D8/R8`, `android.jar` (API 35), a Temurin 21 JDK if Java isn't already installed, and `bundletool` (with `--bundletool`). Builds also fetch the `android.jar` for your `compileSdk`. Pass `--kotlin`, `--ksp`, or `--native` to fetch `kotlinc`, KSP2, or CMake, Ninja, and the NDK. Everything stays inside `~/.apkforge` (move it with `APKFORGE_HOME`), every download checks against SHA-256 pins in `pins.json`, and `--offline` blocks downloads outright.

Upstream sources are pinned: static `aapt2` from `lzhiyong/android-sdk-tools`, `android.jar` stubs from `Sable/android-platforms`, and JDKs from Eclipse Temurin. Libraries come from Google Maven and Maven Central unless you vendor them.

**Java lookup (`1.3.x`):** Community Edition checks `JAVA_HOME`, then `PATH`, then `~/.apkforge/toolchain/jdk`, downloading Temurin 21 without root if needed. In **Enterprise `1.6.0`**, `aapt2` and `d8` run natively in pure Python; in **Enterprise `1.7.0`**, pure-Python `javac` drops the JDK requirement completely for Java builds.

---

## Enterprise Edition (`1.6.0` & `1.7.0`)

The **apkforge Enterprise Engine** takes the Community foundation and finishes removing the heavy external toolchain. Built for cloud CI workers, AI app-builder backends, ARM64 edge nodes, and large multi-module Android teams:

* **Byte-for-Byte Pure-Python `aapt2` & `d8` (`1.6.0`):** Clean-room Python compilers matching Google's official `aapt2` (`resources.arsc`, binary XML, 9-patch PNG crunching) and `d8` (`classes.dex`, Java 8+ desugaring, interface desugaring) outputs byte-for-byte. Zero C++ binaries, zero JVM invocation.
* **Zero-SDK ARM64 & On-Device Builds (`1.6.0`):** Compile real Android apps straight on ARM64 Linux servers, Raspberry Pis, or Android phones (Termux/Pydroid) without hunting for architecture-locked SDK binaries or rooting anything.
* **Dual-Engine Runtime Switching (`1.6.0`):** Flip between Pure-Python Mode (tiny RAM footprint, byte-for-byte parity) and Host Toolchain Mode right inside `apkforge.json`.
* **Built-In Mock `android.jar` & Standalone Test Runners (`1.6.0`):** Pure-Python framework stub generation paired with native JUnit and instrumented test runners. Run unit tests fast without hauling gigabytes of platform JARs.
* **Full Multi-Module Gradle Parity (`1.6.0`):** Product flavors and flavor dimensions, build-type source sets (`src/debug/`, `debugImplementation`), Hilt `@AndroidEntryPoint` bytecode rewriting, Navigation Safe Args, Data Binding, AIDL, `packagingOptions`, and ABI/density splits.
* **Firebase & Google Services (`1.6.0`):** Native `google-services.json` resource generation (`src/<buildType>/`, `src/`, and module lookup order), Crashlytics R8 mapping ID generation, and `apkforge crashlytics-upload`.
* **Zero-JDK Pure-Python `javac` (`1.7.0`, Dropping Next):** Compiles Java source straight to `.class` bytecode in pure Python. No JVM cold start, no 2 GB build images, just fast serverless builds.
* **Security & Compliance:** KMS/HSM signing (keys never touch build machines), ECDSA keys, APK Signature Scheme v3 key rotation, SLSA build provenance, CycloneDX SBOMs, remote content-addressed build caching, Google Play Publishing API uploads, and signing audit logs.

---

## For companies

**Community Edition (`1.3.x`)** is Apache-2.0 and free for commercial use.

**Enterprise Engine (`1.6.0+`)** and engineering services are for teams building Android apps at scale, AI app-generation platforms burning cash on Gradle containers, and security-sensitive mobile teams:

* **Enterprise Licensing & `1.7.0` Early Access:** Commercial rights to the byte-for-byte pure-Python compiler suite (`1.6.0`), early access to zero-JDK `javac` (`1.7.0`), embedded cloud worker deployment, and enterprise compliance features (KMS/HSM signing, SLSA provenance, CycloneDX SBOMs, remote build cache).
* **Priority Support:** Direct line for build failures and release blockers with an agreed response SLA.
* **Migration & CI Setup:** Moving off Gradle with `import-gradle`, setting up air-gapped builds with `vendor`, and locking down reproducible CI pipelines.
* **Sponsored Features:** Fund priority roadmap items or custom integrations on your own timeline.
* **Android CI Audit:** Fixed-fee audit of your existing Android CI/CD setup (build times, caching, signing security, reproducibility), delivered as a written report with prioritized fixes.

Building Android APKs or App Bundles in production and want to license `1.6.0` / `1.7.0` or see if apkforge fits your stack? Email **[gnana.sagar.research@gmail.com](mailto:gnana.sagar.research@gmail.com)** with a couple lines about your setup. Happy to jump on a free 30-minute call if you're seriously evaluating it.

---

## Support the project

apkforge Community Edition (`1.3.x`) is free and open source under Apache-2.0, built and maintained independently. If it saves your team hours or server spend, sponsoring keeps it moving. Stars, bug reports, and telling a colleague help a ton too.

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

Double-check the first and last few characters after pasting, and send only on the network listed above.

---

## Contributing

Bug reports, ideas, and pull requests are welcome. If you hit a build issue, drop the command you ran along with the output of `apkforge doctor`. To hack on apkforge itself, see [Development](#development).

---

## Development

```bash
python3 -m unittest discover -s tests    # 64 tests, under a minute with the toolchain installed
```

Tests that build demo apps skip themselves until the toolchain is present: run `apkforge doctor --bundletool`, then build any app once so the API 36 `android.jar` is downloaded.

```bash
python3 tools/build_pyz.py . dist/apkforge-1.3.0.pyz   # single-file build (carries LICENSE and NOTICE)
python3 -m pip install build && python3 -m build        # wheel and sdist into dist/
tools/bare_env.sh setup fresh && tools/bare_env.sh run fresh python3 -m apkforge doctor   # test with no Java
```

The version lives in `apkforge/util.py` (`VERSION`, plus `BUILD` for pre-release labels) and `pyproject.toml`; `tests/test_packaging.py` fails if they drift out of sync.

---

## License

Community Edition (`1.3.x`) is licensed under the **Apache License 2.0**. See [LICENSE](https://github.com/invarq/apkforge/blob/main/LICENSE) and [NOTICE](https://github.com/invarq/apkforge/blob/main/NOTICE). Copyright 2026 Gnana Sagar.
