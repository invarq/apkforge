# Changelog

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
