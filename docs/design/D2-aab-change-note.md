# apkforge 1.2.0 (dev9): Android App Bundles

> **Shipped in apkforge 1.3.0.** This note was written for the 1.2.0 dev9 pre-release; everything it describes is in 1.3.0 unchanged. See `CHANGELOG.md`.

dev8 built an unsigned bundle and could not check one. dev9 signs it, reads it back, tells you what Google Play will object to, and turns it into an APK you can try out.

## Commands

    apkforge build myapp --aab                 build the bundle, sign it, check it (the debug key, unless --release)
    apkforge build myapp --release --aab       signed with "uploadSigning", else "signing", else the debug key (then Google Play refuses it and the log says so)
    apkforge build myapp --aab --no-sign-aab   leave it unsigned (it is signed somewhere else)
    apkforge keygen myapp --upload             optional: upload.p12 (alias upload, password in $APKFORGE_UPLOAD_PASS) and the "uploadSigning" section
    apkforge verify x.aab                      JSON on stdout, log on stderr, exit 1 when the signature or the structure is wrong
    apkforge verify x.aab --play               ... and when Google Play will refuse it (debug certificate, android:debuggable, unsigned)
    apkforge verify x.aab --allow-unsigned     an unsigned bundle is not a failure
    apkforge test-bundle myapp [--install]     bundletool's universal APK of the bundle, signed with the normal "signing" / debug key and checked; --install runs it on a device over adb
    apkforge test-bundle x.aab                 the same for a bundle that exists (debug key)

`--serial`, `--adb`, `--reinstall` and `--logcat` are about the device, so `test-bundle` refuses them without `--install` (it does not ignore them). With `--json` the app's log (`--logcat`) goes to stderr, so stdout stays one JSON document.

## Which key signs what
- The APK, and the universal APK of `test-bundle`: "signing", else the debug key (unchanged from dev8).
- The bundle, debug build: the debug key. Release build: "uploadSigning" when the module has that section (same fields as "signing"), else "signing", else the debug key.
- "uploadSigning" is checked when the project loads: unknown keys, a missing "keystore" or a non-string value are errors, not a silent fallback to another key.

## What is checked, and by whom
Each claim carries the usual tag, so you can see who looked.
- signature: apkforge's strict reader (every file listed and matching its digest, every manifest section covered, the PKCS#7 block verifies; SHA-256/384/512 accepted, SHA-1 only warns) [internal], and `jarsigner -verify` when a JDK is installed [internal + jarsigner].
- structure: `bundletool validate` [bundletool].
- manifest and BundleConfig.pb: decoded by apkforge's own protobuf reader and compared with `bundletool dump manifest` / `dump config` [internal + bundletool]. A disagreement fails the build.
- `build --aab` fails when a reader rejects the bundle. `verify` never downloads: jarsigner and bundletool are used when they are already there, and the claim says "[internal only; ...]" when they are not.
- `build --json` keeps `"aab"` as the path; the details are under the new key `"aab_report"` (signed_with, signature, bundle, play, oracles, claims).

## Google Play findings (logged by build, listed by verify under "play")
- errors: not signed, signed with the debug certificate, `android:debuggable="true"`. Release builds warn about them, debug builds only mention them; `build` never fails on them. `verify --play` turns them into failures.
- warnings: certificate valid only until 22 October 2033 or earlier, a `com.example` package, several signatures, SHA-1-only digests.
- Evidence: the debug-certificate rule is on developer.android.com/studio/publish/app-signing. The debuggable rule rests on community reports (no official page was found). The 2033 rule is a warning because Google's page does not say it applies to an upload key.

## optimizeResources reaches the bundle (release builds)
sparseEncoding -> `VARIANT_FOR_SDK_32` (a variant for SDK 32+, so no minSdk gate); collapseNames + keep + dedupe -> `collapsedResourceNames` (the "keep" names must be resources, and the getIdentifier guard runs, as for the APK); shortenPaths has no bundletool option (1.18.3): the log says so, the APK beside the bundle is still shortened. React Native's uncompressed glob is still passed. The "bundle's resources are not optimized here" message is gone.

## Files
New: `apkforge/aab.py` (protobuf reader, bundle decoders, Play findings, bundle config, universal APK). Changed: `signing.py` (sign_aab, verify_aab and the DER helpers), `build.py`, `cli.py`, `project.py`, `resopt.py` (check_keep split out of plan), `toolchain.py` (jarsigner()), `pins.json` (bundletool 1.18.3, sha256 a099cfa1543f55593bc2ed16a70a7c67fe54b1747bb7301f37fdfd6d91028e29), `util.py` (BUILD = "dev9").

## Tested
`python3 -m unittest discover -s tests`: 45 tests. 37 run anywhere (protobuf reader, decoders, config mapping, Play findings, signer round trip and tampering, universal-APK layout, config validation, key selection, CLI exit codes and flag checks). 8 build demo apps into bundles and check them with the real jarsigner (JDK 21) and bundletool 1.18.3: debug bundle, --no-sign-aab, verify, a release build with keygen + keygen --upload + optimizeResources (sparse + collapse) signed by the upload key, test-bundle on a project and on a .aab, and test-bundle --install against a stand-in `adb` script: the calls it makes and their order, the universal APK the phone is given, an install the phone refuses (told about --reinstall, never uninstalls on its own; with --reinstall: uninstall, install again), no phone or an unauthorized one (stops before the build), --logcat.

## Not tested
- `test-bundle --install` on a real phone or emulator: there is no device here. It is tested against a stand-in adb script, and it uses the same adb functions as `apkforge run`.
- What the bundle config does to the APKs bundletool makes from it, and whether Google Play accepts the bundle (no Play Console here).
