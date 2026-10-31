"""16 KB page-size check for the native libraries in an APK.

Google Play requires apps that target Android 15 (API 35) or higher to run on devices with 16 KB memory pages. For a native library that is a
property of the ELF file itself, separate from how the zip entry is aligned:

  align       every PT_LOAD segment is aligned to at least 16 KB (p_align >= 2**14); a library linked for 4 KB pages (the NDK r27 and older default) is not
  congruence  a segment's file offset and virtual address must agree modulo 16 KB, or it cannot be mapped at that alignment
  relro       when the PT_GNU_RELRO range shares its PT_LOAD segment with later writable data (.data, .bss), the range must END on a 16 KB boundary:
              the loader makes it read-only in whole pages, so a range that ends mid-page makes the app crash with SIGSEGV on a 16 KB device.
              A RELRO range that fills its segment to the end cannot reach any later data, so it is fine (measured with clang/lld and gcc/ld.bfd).

The rule applies to the 64-bit ABIs (arm64-v8a, x86_64, riscv64): Google's page names only those, and says "on 64-bit devices". A 32-bit library is
read and reported, never failed. Zip alignment is a second, separate requirement (a stored .so must start on a 16 KB boundary inside the APK):
zip_offsets() checks it and zipalign -P 16 is its independent reader.

parse() below is the whole reader: no readelf, no library. readelf (binutils) is the independent second reader: readelf_rows() + compare().
"""
import os
import re
import struct
import subprocess
import tempfile
import zipfile

PAGE = 16384
PT_LOAD, PT_GNU_RELRO = 1, 0x6474E552
MACHINES = {3: "x86", 8: "mips", 20: "ppc", 21: "ppc64", 40: "arm", 62: "x86_64", 183: "aarch64", 243: "riscv"}
ABI_MACHINE = {"arm64-v8a": 183, "x86_64": 62, "armeabi-v7a": 40, "x86": 3, "riscv64": 243}
REQUIRED_ABIS = frozenset(("arm64-v8a", "x86_64", "riscv64"))
LIB_ENTRY = re.compile(r"lib/([^/]+)/([^/]+\.so)\Z")


class ElfError(ValueError):
    """the bytes are not an ELF file this reader can use"""


# ------------------------------------------------------------------------------------------------------------- reader
def parse(data):
    """ELF header + program headers.  -> {"bits": 32|64, "little": bool, "type": int, "machine": int,
    "segments": [{"type", "flags", "offset", "vaddr", "filesz", "memsz", "align"}]}.  Raises ElfError."""
    if len(data) < 52 or data[:4] != b"\x7fELF":
        raise ElfError("not an ELF file")
    bits, end = {1: 32, 2: 64}.get(data[4]), {1: "<", 2: ">"}.get(data[5])
    if not bits or not end:
        raise ElfError("unknown ELF class %d / byte order %d" % (data[4], data[5]))
    try:
        etype, machine = struct.unpack_from(end + "HH", data, 16)
        if bits == 64:
            phoff = struct.unpack_from(end + "Q", data, 32)[0]
            phentsize, phnum = struct.unpack_from(end + "HH", data, 54)
            want = 56
        else:
            phoff = struct.unpack_from(end + "I", data, 28)[0]
            phentsize, phnum = struct.unpack_from(end + "HH", data, 42)
            want = 32
        if phnum == 0xFFFF:
            raise ElfError("more than 65534 program headers (PN_XNUM) is not supported")
        if phnum and phentsize != want:
            raise ElfError("program header entry size is %d, a %d-bit ELF file has %d" % (phentsize, bits, want))
        if phoff + phnum * phentsize > len(data):
            raise ElfError("the program header table runs past the end of the file (truncated?)")
        segs = []
        for i in range(phnum):
            o = phoff + i * phentsize
            if bits == 64:
                t, fl, off, va, _pa, fs, ms, al = struct.unpack_from(end + "IIQQQQQQ", data, o)
            else:
                t, off, va, _pa, fs, ms, fl, al = struct.unpack_from(end + "IIIIIIII", data, o)
            segs.append({"type": t, "flags": fl, "offset": off, "vaddr": va, "filesz": fs, "memsz": ms, "align": al})
    except struct.error as e:
        raise ElfError("truncated ELF file (%s)" % e)
    return {"bits": bits, "little": end == "<", "type": etype, "machine": machine, "segments": segs}


def machine_name(m):
    return MACHINES.get(m, "machine %d" % m)


# ------------------------------------------------------------------------------------------------------------- rules
def check(data, abi=None, page=PAGE):
    """Apply the rules above to one library.  abi = the lib/<abi>/ directory it sits in (None: judged by its ELF class).
    -> {"abi", "bits", "machine", "required", "ok", "findings": [{"code","text"}], "warnings": [str], "loads": [{"vaddr","align"}], "relro": {...}|None}
    `findings` lists every rule the library breaks; `ok` is False only when the library is `required` (64-bit) and has any.
    Raises ElfError when the bytes are not an ELF file."""
    elf = parse(data)
    required = (abi in REQUIRED_ABIS) if abi is not None else elf["bits"] == 64
    segs = elf["segments"]
    loads = [s for s in segs if s["type"] == PT_LOAD]
    findings, warnings = [], []
    low = [s for s in loads if s["align"] < page]
    if low:
        smallest = min(s["align"] for s in low)
        findings.append({"code": "align", "text": "%d of %d PT_LOAD segments are aligned to %s, not %s: link with -Wl,-z,max-page-size=16384 -Wl,-z,common-page-size=16384 "
                                                  "(NDK r28 and newer do this by default)" % (len(low), len(loads), _kb(smallest), _kb(page))})
    for i, s in enumerate(loads):
        if s["align"] >= page and (s["vaddr"] - s["offset"]) % page:       # below 16 KB the segment is judged by `align` alone (a 4 KB segment is congruent modulo 4 KB)
            findings.append({"code": "congruence", "text": "PT_LOAD #%d: virtual address 0x%x and file offset 0x%x differ modulo 16 KB, so it cannot be mapped on 16 KB pages"
                                                           % (i, s["vaddr"], s["offset"])})
    relro = next((s for s in segs if s["type"] == PT_GNU_RELRO), None)
    relro_info = None
    if relro:
        end = relro["vaddr"] + relro["memsz"]
        host = next((s for s in loads if s["vaddr"] <= relro["vaddr"] < s["vaddr"] + max(s["memsz"], 1)), None)
        shares = bool(host) and end < host["vaddr"] + host["memsz"]
        relro_info = {"vaddr": relro["vaddr"], "end": end, "end_aligned": end % page == 0, "shares_load": shares}
        if shares and end % page:
            findings.append({"code": "relro", "text": "PT_GNU_RELRO ends at 0x%x, which is not on a 16 KB boundary, and later writable data follows it in the same segment: "
                                                      "the app crashes with SIGSEGV on a 16 KB device (link with -Wl,-z,common-page-size=16384)" % end})
    want = ABI_MACHINE.get(abi)
    if want is not None and elf["machine"] != want:
        warnings.append("this is a %s library (ELF machine %d) but it sits in lib/%s/" % (machine_name(elf["machine"]), elf["machine"], abi))
    if abi is not None and abi not in ABI_MACHINE:
        warnings.append("lib/%s/ is not an ABI directory Android knows" % abi)
    return {"abi": abi, "bits": elf["bits"], "machine": machine_name(elf["machine"]), "required": required, "ok": not (required and findings),
            "findings": findings, "warnings": warnings,
            "loads": [{"vaddr": s["vaddr"], "align": s["align"]} for s in loads], "relro": relro_info}


def _kb(n):
    return "%d KB" % (n // 1024) if n >= 1024 and n % 1024 == 0 else "%d bytes" % n


# ------------------------------------------------------------------------------------------------------------- the APK
def zip_offsets(apk, names=None):
    """-> [(entry name, offset of its data)] for every STORED lib/<abi>/*.so entry whose data does not start on a 16 KB boundary of the file.
    A compressed entry is skipped: the installer extracts it. names = only these entries."""
    bad = []
    with zipfile.ZipFile(apk) as z, open(apk, "rb") as f:
        for zi in z.infolist():
            if not LIB_ENTRY.match(zi.filename) or (names is not None and zi.filename not in names) or zi.compress_type != zipfile.ZIP_STORED:
                continue
            f.seek(zi.header_offset)
            head = f.read(30)
            if len(head) < 30 or head[:4] != b"PK\x03\x04":
                bad.append((zi.filename, -1))
                continue
            name_len, extra_len = struct.unpack_from("<HH", head, 26)
            off = zi.header_offset + 30 + name_len + extra_len
            if off % PAGE:
                bad.append((zi.filename, off))
    return bad


def scan_apk(apk):
    """Check every lib/<abi>/*.so entry of an APK.  -> [{"name", "stored", "size", ... check() ... } | {"name", "error"}], in archive order."""
    out = []
    with zipfile.ZipFile(apk) as z:
        for zi in z.infolist():
            m = LIB_ENTRY.match(zi.filename)
            if not m:
                continue
            data = z.read(zi)
            try:
                r = check(data, m.group(1))
            except ElfError as e:
                r = {"abi": m.group(1), "error": str(e)}
            r.update(name=zi.filename, stored=zi.compress_type == zipfile.ZIP_STORED, size=len(data))
            r["_data"] = data                      # kept for the readelf cross-check; dropped by strip()
            out.append(r)
    return out


def strip(libs):
    """the libs list without the raw bytes (fit for JSON)"""
    return [{k: v for k, v in r.items() if k != "_data"} for r in libs]


# ------------------------------------------------------------------------------------------------------------- readelf
_ROW = re.compile(r"^\s+(LOAD|GNU_RELRO)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+.*?(0x[0-9a-fA-F]+)\s*$")


def readelf_rows(readelf, data):
    """What `readelf -lW` says about the LOAD and GNU_RELRO headers of these bytes: [(name, offset, vaddr, filesz, memsz, align)], or None when
    readelf could not read them."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "lib.so")
        with open(p, "wb") as f:
            f.write(data)
        try:
            r = subprocess.run([readelf, "-lW", p], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, env=dict(os.environ, LC_ALL="C"))
        except (OSError, subprocess.SubprocessError):
            return None
    if r.returncode != 0:
        return None
    rows = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        m = _ROW.match(line)
        if m:
            g = m.groups()
            rows.append((g[0],) + tuple(int(x, 16) for x in (g[1], g[2], g[4], g[5], g[6])))      # offset, vaddr, filesz, memsz, align (the physical address is skipped)
    return rows


def own_rows(data):
    """the same rows from parse(), in the same order and shape as readelf_rows()"""
    names = {PT_LOAD: "LOAD", PT_GNU_RELRO: "GNU_RELRO"}
    return [(names[s["type"]], s["offset"], s["vaddr"], s["filesz"], s["memsz"], s["align"]) for s in parse(data)["segments"] if s["type"] in names]


def compare(data, rows):
    """-> [] when readelf's rows equal ours, else one line per difference (an apkforge bug, or a file one of the two reads differently)"""
    mine = own_rows(data)
    if mine == rows:
        return []
    out = []
    for i in range(max(len(mine), len(rows))):
        a, b = mine[i] if i < len(mine) else None, rows[i] if i < len(rows) else None
        if a != b:
            out.append("header %d: apkforge read %s, readelf read %s" % (i, _row(a), _row(b)))
    return out


def _row(r):
    return "nothing" if r is None else "%s off=0x%x vaddr=0x%x filesz=0x%x memsz=0x%x align=0x%x" % r
