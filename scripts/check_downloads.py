"""Check that every pre-downloaded data item is present and complete (run it after copying the project to the server).

    python scripts/check_downloads.py            # fast: sizes, shard lists, zip CRCs, image blob digests
    python scripts/check_downloads.py --deep     # + decompressed layer digests of the images (as docker load does)

Exit code 0 = everything run_all.py needs is present (it then downloads nothing except what is missing).
"""
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def main(deep=False):
    import yaml
    from race.infra import model_complete, model_dir
    import fetch_images
    cfg = yaml.safe_load(open(ROOT / "configs" / "default.yaml", encoding="utf-8"))
    rows, ok_all = [], True

    def add(name, ok, info):
        nonlocal ok_all
        ok_all &= bool(ok)
        rows.append((name, "OK" if ok else "MISSING", info))

    for f in ("test_webarena_lite.raw.json", "webarena_test.raw.json"):
        p = ROOT / "data" / f
        n = len(json.load(open(p, encoding="utf-8"))) if p.exists() else 0
        add(f"task table {f}", n in (165, 812), f"{n} tasks")
    for m in dict.fromkeys([cfg["llm"]["model"]] + [b["model"] for b in cfg.get("extra_backbones", [])]):
        d = model_dir(m)
        gb = sum(x.stat().st_size for x in d.glob("*.safetensors")) / 1e9 if d.exists() else 0
        add(f"model {m}", model_complete(d), f"{gb:.1f} GB in {d.relative_to(ROOT)}")
    for s in fetch_images.SITES:
        d = fetch_images.OUT / s
        ok = (d / "COMPLETE").exists() and (d / "manifest.json").exists()
        if ok:  # every blob matches its registry digest (= its file name); --deep: decompressed layer digests too
            man = json.loads((d / "manifest.json").read_text())[0]
            ok = all((d / n).exists() and fetch_images.sha256_file(d / n) == "sha256:" + n.split(".")[0]
                     for n in [man["Config"]] + man["Layers"])
            if ok and deep:
                ok = fetch_images.verify_site(d)
        gb = sum(x.stat().st_size for x in d.glob("*")) / 1e9 if d.exists() else 0
        add(f"image webarena-verified-{s}", ok, f"{gb:.1f} GB in {d.relative_to(ROOT)}")
    for z in ("chromium-1117-chromium-linux.zip", "ffmpeg-1009-ffmpeg-linux.zip"):
        p = ROOT / "downloads" / "playwright_linux" / z
        ok = p.exists() and zipfile.ZipFile(p).testzip() is None
        add(f"playwright {z}", ok, f"{p.stat().st_size / 1e6:.0f} MB" if p.exists() else "")
    pj = ROOT / "downloads" / "chromium_libs" / "packages.json"
    pkgs = json.loads(pj.read_text()) if pj.exists() else {}
    deb = ROOT / "downloads" / "chromium_libs" / "debs"
    ok = bool(pkgs) and all((deb / v["file"]).exists() and fetch_images.sha256_file(deb / v["file"]) == "sha256:" + v["sha256"]
                            for v in pkgs.values())
    add("Chromium system libraries (Ubuntu 20.04 .deb)", ok, f"{len(pkgs)} packages")
    sums = ROOT / "downloads" / "models" / "SHA256SUMS.json"
    if deep and sums.exists():
        bad = [k for k, v in json.loads(sums.read_text()).items()
               if fetch_images.sha256_file(ROOT / "downloads" / "models" / k) != "sha256:" + v]
        add("model files sha256 (Hugging Face)", not bad, f"mismatch: {bad}" if bad else "all match")
    whl = list((ROOT / "downloads" / "wheels").glob("*.whl"))
    add("Linux cp312 wheels", any("playwright-1.44.0" in w.name for w in whl), f"{len(whl)} wheels")
    nl = ROOT / "downloads" / "nltk_data" / "tokenizers"
    add("NLTK punkt / punkt_tab", (nl / "punkt").is_dir() and (nl / "punkt_tab").is_dir(), str(nl.relative_to(ROOT)))
    w = max(len(r[0]) for r in rows)
    for name, st, info in rows:
        print(f"{name:<{w}}  {st:<7}  {info}")
    print("ALL COMPLETE" if ok_all else "INCOMPLETE (run_all.py downloads the missing items itself if the server has "
                                        "internet; or re-run the downloads here)")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(deep="--deep" in sys.argv))
