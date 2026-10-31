"""Packaging guards: the two facts pip depends on, checked where the tests already run.

1. The version is written twice: util.VERSION + util.BUILD feed `apkforge --version`, pyproject.toml feeds pip.
   They must name the same release in PEP 440 spelling ("1.2.0" + "dev9" -> "1.2.0.dev9").
2. pins.json must ship inside the package. toolchain._pins() answers {} when it cannot read the file, and every download then falls back
   from "SHA-256 pin" to "trust on first use" without a word. A wheel without pins.json would work and be weaker; this test makes that loud.
"""
import os
import re
import unittest

from apkforge import toolchain, util

PYPROJECT = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "pyproject.toml")


def pep440(version, build):
    """util.VERSION + util.BUILD as a PEP 440 version: ("1.2.0", "dev9") -> "1.2.0.dev9", ("1.2.0", "rc1") -> "1.2.0rc1", ("1.2.0", "") -> "1.2.0"."""
    if not build:
        return version
    if re.fullmatch(r"dev\d+", build):
        return "%s.%s" % (version, build)
    if re.fullmatch(r"(?:a|b|rc)\d+", build):
        return version + build
    raise ValueError("util.BUILD %r has no PEP 440 spelling in pep440(): extend it" % build)


class PackagingTests(unittest.TestCase):
    def test_pyproject_version_matches_util(self):
        if not os.path.isfile(PYPROJECT):
            self.skipTest("no pyproject.toml beside tests/ (these tests are running from an installed copy)")
        with open(PYPROJECT, encoding="utf-8") as f:
            m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
        self.assertIsNotNone(m, "pyproject.toml needs a static version line")
        self.assertEqual(m.group(1), pep440(util.VERSION, util.BUILD),
                         "pyproject.toml and util.VERSION/util.BUILD name different releases: change them together")

    def test_pins_json_is_shipped_and_read(self):
        pins = toolchain.PINS
        self.assertTrue(pins, "pins.json was not found or is empty: downloads would silently lose their SHA-256 pins")
        for key in ("bundletool", "kotlin", "jadx"):
            self.assertIn(key, pins)
        for key, digest in pins.items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$", "pin %s is not a SHA-256 hex digest" % key)


if __name__ == "__main__":
    unittest.main()
