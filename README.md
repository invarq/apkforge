# apkforge

**Build Android apps without Gradle, Android Studio or the SDK Manager.**

[![PyPI](https://img.shields.io/pypi/v/apkforge.svg)](https://pypi.org/project/apkforge/)
[![Python versions](https://img.shields.io/pypi/pyversions/apkforge.svg)](https://pypi.org/project/apkforge/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/invarq/apkforge/blob/main/LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](https://github.com/invarq/apkforge#status-and-known-limitations)
[![Sponsor](https://img.shields.io/badge/sponsor-%E2%9D%A4-ea4aaa.svg?logo=githubsponsors)](https://github.com/sponsors/invarq)

apkforge is a Python tool with **no dependencies** (standard library only). It drives `aapt2`, `javac`/`kotlinc` and D8/R8 directly, signs APKs and Android App Bundles with its own pure-Python signature engine, and downloads a pinned toolchain the first time it needs one. A bare machine, such as a plain Docker image, needs only Python and network access.

```bash
pip install apkforge
apkforge new myapp --package com.acme.myapp   # a project that builds as-is
apkforge build myapp                          # debug APK -> myapp/build/outputs/
```

The first run downloads the toolchain into `~/.apkforge`.

<p align="center">
  <a href="https://github.com/invarq/apkforge/blob/main/docs/assets/apkforge-film-1080p60.mp4">
    <img src="https://raw.githubusercontent.com/invarq/apkforge/main/docs/assets/apkforge-reveal.gif" width="640" alt="The apkforge logo: a spark strikes an anvil-shaped green A, then the APKForge wordmark appears">
  </a>
  <br>
  <sub>Watch the <a href="https://github.com/invarq/apkforge/blob/main/docs/assets/apkforge-film-1080p60.mp4">16-second film</a> (1080p, with sound): the logo, then a build of the starter template, sped up.</sub>
</p>

## Why apkforge

- **Nothing to set up.** Python 3.9 or newer and a network connection are all it needs to build. apkforge fetches aapt2, D8/R8, an `android.jar` and, when the machine has no Java, a Temurin 21 JDK, all into `~/.apkforge` and without root.
- **No Python dependency tree.** It uses the standard library only, so there is nothing to install or audit on the Python side.
- **Checked downloads, offline when you need it.** Downloads are verified against SHA-256 pins or the publisher's own checksum, and otherwise recorded on first use. `--offline` forbids downloads, and `apkforge vendor` copies every dependency into `vendor/` so builds can run without a network.
- **Built with Google Play in mind.** `apkforge build --release --aab` produces a signed APK and App Bundle and prints what Google Play will object to. `apkforge verify --play` turns those objections into a failing exit status.
- **Bring your existing project.** `apkforge import-gradle` writes `apkforge.json` from a Gradle project, and it does not need Gradle to do it.

## Install

```bash
pip install apkforge                  # from PyPI, Python 3.9+
uvx apkforge --help                   # or run it without installing, via uv
python3 apkforge-1.3.1.pyz --help     # or the single-file build: nothing to install
```

Build the single-file `.pyz` yourself with `python3 tools/build_pyz.py . dist/apkforge-1.3.1.pyz` (see [Development](https://github.com/invarq/apkforge#development)).

## Quick start

```bash
apkforge new myapp --package com.acme.myapp     # a project that builds as-is (API 36, minSdk 24)
apkforge build myapp                            # debug APK, checked after the build -> myapp/build/outputs/
apkforge run myapp                              # build, install over adb, start it (--logcat follows its log)

apkforge keygen myapp                           # release keystore and "signing" section (the password is printed once)
read -rs APKFORGE_STORE_PASS; export APKFORGE_STORE_PASS     # type the password; it stays out of your shell history
apkforge build myapp --release --aab            # signed APK and App Bundle; what Google Play will object to is printed
apkforge verify myapp/build/outputs/*.aab       # JSON report on stdout; --play fails on what Google Play refuses
apkforge test-bundle myapp [--install]          # bundletool's universal APK of the bundle, signed and checked
```

## Commands

| Command | What it does |
|---|---|
| `new` | Create a project that builds as-is |
| `build` | Build an APK, or an AAR for a library module (`--release`, `--aab`) |
| `run` | Build, install on a device or emulator over adb, and start the app |
| `keygen` | Create a release keystore and the matching `signing` section in `apkforge.json` (`--upload` adds a separate upload key) |
| `verify` | Verify an APK or bundle (signatures, alignment) and say which claims an external tool confirmed |
| `test-bundle` | Try an App Bundle out locally through bundletool's universal APK, signed and checked |
| `crashlytics-upload` | Send a release build's R8 mapping to Firebase Crashlytics through the Firebase CLI (`--dry-run` prints the command) |
| `import-gradle` | Write `apkforge.json` from an existing Gradle project (no Gradle needed) |
| `vendor` | Copy every dependency file into `vendor/` (Maven layout) so builds can run offline |
| `publish` | Publish a library module into a Maven-layout directory |
| `doctor` | Provision and check the toolchain |
| `clean` | Delete `.apkforge/` and `build/` |

`apkforge --help` and `apkforge <command> --help` list every option.

## What gets downloaded

`apkforge doctor` downloads and checks what a build needs: aapt2, D8/R8, an `android.jar` (API 35), a JDK when the machine has none, and, with `--bundletool`, bundletool for App Bundles. Builds also fetch the `android.jar` of the project's `compileSdk`. Everything lives in `~/.apkforge` (set `APKFORGE_HOME` to move it). A download is checked against the SHA-256 pins in `pins.json` or the checksum its publisher ships, and is otherwise recorded on first use. `--offline` forbids downloads.

**Java.** The tools apkforge drives (D8/R8, kotlinc, bundletool) are Java programs, so a Java runtime is needed. apkforge looks for `java` in `JAVA_HOME`, then on `PATH`, then in the JDK it downloaded itself (`~/.apkforge/toolchain/jdk`). When there is none, it downloads a Temurin 21 JDK there, without root, the first time one is needed or when you run `apkforge doctor`. With `--offline` nothing is downloaded and the error says so.

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

The resource rules and the search order come from Google's plugin source, and the Firebase CLI's arguments from its source. What has not been tried is listed under
[Status and known limitations](https://github.com/invarq/apkforge#status-and-known-limitations).

## Status and known limitations

**Version 1.3.1, alpha.** apkforge builds real projects, but it is not a drop-in Gradle replacement yet. What does not work today:

- Hilt: its Gradle plugin rewrites the base class of `@AndroidEntryPoint` classes in bytecode, and apkforge has no such step.
- Product flavors and flavor dimensions.
- Build-type source sets and dependencies (`src/debug/`, `debugImplementation`), and build types other than debug and release.
- RenderScript.
- ECDSA signing keys (RSA only).

`apkforge import-gradle` warns about these when it meets them. Not tested yet: `test-bundle --install` on a real device (only against a stand-in adb), whether Google Play accepts a bundle ([design note](https://github.com/invarq/apkforge/blob/main/docs/design/D2-aab-change-note.md)), the Crashlytics mapping upload against the real backend, and a build against Google's own Firebase artifacts (the Firebase tests use stand-in jars). apkforge is developed and tested on Linux.

## Roadmap

The gaps above are the roadmap, together with testing on real devices, on Google Play and on other operating systems. Sponsors and company customers help decide the order, so tell me what blocks you: [open an issue](https://github.com/invarq/apkforge/issues) or see [For companies](https://github.com/invarq/apkforge#for-companies).

## Support the project

apkforge is free and open source under Apache-2.0, and it is built and maintained independently. If it saves you or your team time, sponsoring helps keep it maintained and pays for the roadmap above. Stars, bug reports and telling a colleague help too.

**[Sponsor on GitHub](https://github.com/sponsors/invarq)** by card, monthly or one-time.

### Crypto

Bitcoin (Taproot, on-chain):

```text
bc1ps5e479g9swmthd3ly4es6hhdkh2ghc54wdvjxqqcjm0cdx7y25jsnee7ts
```

Ethereum mainnet (ETH and ERC-20 tokens such as USDC and USDT):

```text
0x1C4e30151698cf3C4D0f7f1435D675b994968217
```

| Bitcoin (Taproot) | Ethereum |
|:---:|:---:|
| ![QR code for the Bitcoin Taproot address](https://raw.githubusercontent.com/invarq/apkforge/main/docs/assets/donate-btc-taproot.png) | ![QR code for the Ethereum address](https://raw.githubusercontent.com/invarq/apkforge/main/docs/assets/donate-eth.png) |

Check the first and last few characters after you paste, and send only on the network named above: crypto sent on the wrong network usually cannot be recovered.

## For companies

apkforge is Apache-2.0, so you can use it commercially at no cost. If your team builds release APKs or App Bundles with it, I can also help with:

- **Priority support.** A direct line to the maintainer for build failures and release blockers, with a response time agreed up front.
- **Sponsored features.** Fund an item from the roadmap, such as Hilt, product flavors or ECDSA signing, and have it built first.
- **Migration and CI setup.** Moving a Gradle project over with `import-gradle`, offline or air-gapped builds with `vendor`, and a pinned, reproducible toolchain in your CI.

Email [REPLACE-WITH-YOUR-EMAIL](mailto:REPLACE-WITH-YOUR-EMAIL) with a line about your project and what you need.

## Contributing

Bug reports, ideas and pull requests are welcome. For a build problem, please include the command you ran and the output of `apkforge doctor`. To work on apkforge itself, see [Development](https://github.com/invarq/apkforge#development) below.

## Development

```bash
python3 -m unittest discover -s tests           # about a minute and a half with the toolchain installed (most of it is the demo builds)
```

The tests that build demo apps skip themselves until the toolchain is there: run `apkforge doctor --bundletool`, then build any app once (`apkforge new /tmp/demo`, `apkforge build /tmp/demo`) so that the API 36 android.jar is downloaded.

```bash
python3 tools/build_pyz.py . dist/apkforge-1.3.1.pyz        # the single-file build (carries LICENSE and NOTICE)
python3 -m pip install build && python3 -m build            # wheel and sdist into dist/
tools/bare_env.sh setup fresh && tools/bare_env.sh run fresh python3 -m apkforge doctor     # a machine with no Java
```

The version is written in `apkforge/util.py` (`VERSION`, and `BUILD` for a pre-release label such as `rc1`, empty for a release) and in `pyproject.toml`; `tests/test_packaging.py` fails when the two differ.

## License

Apache License 2.0. See [LICENSE](https://github.com/invarq/apkforge/blob/main/LICENSE) and [NOTICE](https://github.com/invarq/apkforge/blob/main/NOTICE). Copyright 2026 Gnana Sagar.
