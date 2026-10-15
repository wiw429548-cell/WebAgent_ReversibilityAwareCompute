"""Download Chromium's runtime shared libraries as Ubuntu 20.04 (focal, glibc 2.31) .deb packages, for servers on
which headless Chromium cannot start for lack of system libraries and where there is no root / apt.

    python scripts/fetch_chromium_libs.py      # -> downloads/chromium_libs/debs/*.deb  (+ packages.json)

The package set is the shared-library closure of Playwright's Chromium 125 (build 1117) as reported by `ldd` (all
libraries except glibc / libgcc), resolved against the focal package indexes (first alternative of every dependency,
library packages only). race/infra.py unpacks them with the Python standard library (ar + tar.xz, no dpkg) into
downloads/chromium_libs/root ONLY if Chromium fails to start with the system libraries, and then puts that directory
on LD_LIBRARY_PATH for the Chromium-launching processes.
"""
import hashlib
import json
import lzma
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "downloads" / "chromium_libs" / "debs"
MIRROR = "http://archive.ubuntu.com/ubuntu"
SUITES = ("focal-updates", "focal-security", "focal")   # first match wins (newest first)
COMPONENTS = ("main", "universe")
# `ldd` closure of chrome / libEGL / libGLESv2 / libvk_swiftshader (Ubuntu package names), see the docstring
NEEDED = """libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libavahi-client3 libavahi-common3 libblkid1 libbrotli1
libbsd0 libcairo2 libcap2 libcom-err2 libcups2 libdatrie1 libdbus-1-3 libdrm2 libexpat1 libffi7 libfontconfig1
libfreetype6 libfribidi0 libgbm1 libgcrypt20 libglib2.0-0 libgmp10 libgnutls30 libgpg-error0 libgraphite2-3
libgssapi-krb5-2 libharfbuzz0b libhogweed5 libidn2-0 libk5crypto3 libkeyutils1 libkrb5-3 libkrb5support0 liblz4-1
liblzma5 libmount1 libnettle7 libnspr4 libnss3 libp11-kit0 libpango-1.0-0 libpcre2-8-0 libpcre3 libpixman-1-0
libpng16-16 libselinux1 libsystemd0 libtasn1-6 libthai0 libunistring2 libuuid1 libwayland-server0 libx11-6 libxau6
libxcb-randr0 libxcb-render0 libxcb-shm0 libxcb1 libxcomposite1 libxdamage1 libxdmcp6 libxext6 libxfixes3 libxi6
libxkbcommon0 libxrandr2 libxrender1 libzstd1 zlib1g libxshmfence1 libx11-xcb1 libegl1 libpangocairo-1.0-0
libpangoft2-1.0-0""".split()
SKIP = {"libc6", "libgcc-s1", "libgcc1", "libstdc++6", "libcrypt1"}   # present on every Linux system (glibc / gcc)


def index():
    pk = {}
    for suite in reversed(SUITES):  # later (newer) suites overwrite
        for comp in COMPONENTS:
            url = f"{MIRROR}/dists/{suite}/{comp}/binary-amd64/Packages.xz"
            txt = lzma.decompress(urllib.request.urlopen(url, timeout=300).read()).decode("utf-8", "replace")
            for block in txt.split("\n\n"):
                f = dict(re.findall(r"^([A-Za-z0-9-]+): (.*)$", block, re.M))
                if "Package" in f and "Filename" in f:
                    pk[f["Package"]] = f
            print(f"index {suite}/{comp}: {len(pk)} packages so far", flush=True)
    return pk


def deps(rec):
    out = []
    for field in ("Pre-Depends", "Depends"):
        for alt in filter(None, (x.strip() for x in rec.get(field, "").split(","))):
            out.append(re.sub(r"\s*\(.*?\)|:any", "", alt.split("|")[0]).strip())
    return out


def resolve(pk):
    todo, done, missing = list(NEEDED), {}, []
    while todo:
        n = todo.pop()
        if n in done or n in SKIP:
            continue
        if n not in pk:
            missing.append(n)
            continue
        done[n] = pk[n]
        # follow library dependencies only (no daemons, data, configuration or base-system packages)
        todo += [d for d in deps(pk[n]) if d.startswith("lib") and not d.endswith(("-common", "-data", "-bin"))]
    return done, missing


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    pk = index()
    sel, missing = resolve(pk)
    if missing:
        print(f"not in focal (skipped): {missing}")
    total = 0
    for n, rec in sorted(sel.items()):
        dest = OUT / Path(rec["Filename"]).name
        if not (dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == rec["SHA256"]):
            data = urllib.request.urlopen(f"{MIRROR}/{rec['Filename']}", timeout=300).read()
            if hashlib.sha256(data).hexdigest() != rec["SHA256"]:
                raise RuntimeError(f"{n}: sha256 mismatch")
            dest.write_bytes(data)
        total += dest.stat().st_size
    (OUT.parent / "packages.json").write_text(json.dumps(
        {n: {"version": r["Version"], "file": Path(r["Filename"]).name, "sha256": r["SHA256"]} for n, r in sorted(sel.items())},
        indent=1))
    print(f"{len(sel)} packages, {total / 1e6:.0f} MB -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
