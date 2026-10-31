#!/usr/bin/env python3
"""Build an apkforge .pyz in the format of the dev8 release file.

usage: build_pyz.py SRC_DIR OUT.pyz       (SRC_DIR holds __main__.py and apkforge/)

Format measured on apkforge-1.2.0-dev8.pyz: a "#!/usr/bin/env python3" line, then a zip with __main__.py first and the files of apkforge/ in name order
(pins.json included), then LICENSE and NOTICE when SRC_DIR has them (Apache-2.0 section 4(a) and (d): whoever gets the file gets the license text and the notices with it), all deflated,
Unix mode 0644, timestamp 2026-01-01 00:00:00, no extra fields.  Nothing else (tests, __pycache__) goes in.
"""
import os
import sys
import zipfile


def build(src, out):
    pkg = os.path.join(src, "apkforge")
    names = sorted(f for f in os.listdir(pkg) if os.path.isfile(os.path.join(pkg, f)) and not f.endswith(".pyc"))
    entries = [("__main__.py", os.path.join(src, "__main__.py"))] + [("apkforge/" + f, os.path.join(pkg, f)) for f in names]
    for extra in ("LICENSE", "NOTICE"):
        if os.path.isfile(os.path.join(src, extra)):
            entries.append((extra, os.path.join(src, extra)))
    with open(out, "wb") as raw:
        raw.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
            for arc, path in entries:
                with open(path, "rb") as fh:
                    data = fh.read()
                info = zipfile.ZipInfo(arc, (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o644 << 16
                z.writestr(info, data)
    os.chmod(out, 0o755)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    build(sys.argv[1], sys.argv[2])
    print("wrote", sys.argv[2])
