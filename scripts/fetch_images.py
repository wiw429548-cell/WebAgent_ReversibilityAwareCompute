"""Download the WebArena-Verified site images from Docker Hub WITHOUT docker (any OS), resumable, sha256-verified.

    python scripts/fetch_images.py                 # all four sites -> downloads/docker_images/<site>/
    python scripts/fetch_images.py shopping gitlab # a subset

Each image is stored as an unpacked `docker save` archive (manifest.json + config + gzip layers). On the server,
scripts/setup_webarena.sh streams it into the container runtime without a second copy on disk:
    tar -C downloads/docker_images/shopping -cf - . | docker load      (or podman load)
Images (pinned tag, linux/amd64): am1n3e/webarena-verified-<site>:<TAG> (ServiceNow WebArena-Verified; same site data
and database credentials as the original WebArena images, plus upstream bug fixes).
"""
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "downloads" / "docker_images"
REPO = "am1n3e/webarena-verified-{site}"
TAG = "0.1.0"
SITES = ("shopping", "shopping_admin", "reddit", "gitlab")
REGISTRY = "https://registry-1.docker.io/v2"
ACCEPT = ", ".join(["application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                    "application/vnd.oci.image.manifest.v1+json"])
CHUNK = 8 << 20


def token(repo):
    url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
    return json.load(urllib.request.urlopen(url, timeout=60))["token"]


def get(url, tok, accept=None, rng=None):
    h = {}
    if accept:
        h["Accept"] = accept
    if rng:
        h["Range"] = f"bytes={rng}-"
    req = urllib.request.Request(url, headers=h)
    # the token must not follow the redirect of a blob to the CDN (presigned URL); Range must
    req.add_unredirected_header("Authorization", f"Bearer {tok}")
    return urllib.request.urlopen(req, timeout=300)


def manifest(repo, tag, tok):
    m = json.load(get(f"{REGISTRY}/{repo}/manifests/{tag}", tok, ACCEPT))
    if "manifests" in m:  # multi-platform index -> linux/amd64
        d = next(x["digest"] for x in m["manifests"]
                 if x.get("platform", {}).get("os") == "linux" and x["platform"].get("architecture") == "amd64")
        m = json.load(get(f"{REGISTRY}/{repo}/manifests/{d}", tok, ACCEPT))
    return m


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


def fetch_blob(repo, digest, size, dest, log):
    """resumable download of one blob, verified against its digest"""
    if dest.exists() and dest.stat().st_size == size and sha256_file(dest) == digest:
        return
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(30):
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        try:
            if have < size:
                r = get(f"{REGISTRY}/{repo}/blobs/{digest}", token(repo), rng=have if have else None)
                if have and r.status != 206:  # server ignored the range: start over
                    have = 0
                with open(part, "ab" if have else "wb") as f:
                    for b in iter(lambda: r.read(CHUNK), b""):
                        f.write(b)
            if part.stat().st_size == size and sha256_file(part) == digest:
                part.replace(dest)
                return
            log(f"  {digest[:19]}: size/digest mismatch -> restarting the blob")
            part.unlink()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"  {digest[:19]}: {type(e).__name__} {str(e)[:120]} (attempt {attempt + 1}) -> resuming in 20 s")
            time.sleep(20)
    raise RuntimeError(f"blob {digest} could not be downloaded")


def verify_site(d, log=print):
    """the check `docker load` performs: sha256 of every DECOMPRESSED layer == config rootfs.diff_ids[i]"""
    import gzip
    m = json.loads((d / "manifest.json").read_text())[0]
    diff_ids = json.loads((d / m["Config"]).read_text())["rootfs"]["diff_ids"]
    if len(diff_ids) != len(m["Layers"]):
        return False
    for name, want in zip(m["Layers"], diff_ids):
        h = hashlib.sha256()
        opener = gzip.open if name.endswith(".gz") else open
        with opener(d / name, "rb") as f:
            for b in iter(lambda: f.read(CHUNK), b""):
                h.update(b)
        if "sha256:" + h.hexdigest() != want:
            log(f"  layer {name[:12]}: decompressed digest mismatch")
            return False
    return True


def fetch_site(site, log=print):
    repo = REPO.format(site=site)
    d = OUT / site
    if (d / "manifest.json").exists() and (d / "COMPLETE").exists():
        log(f"{site}: complete ({d})")
        return d
    d.mkdir(parents=True, exist_ok=True)
    m = manifest(repo, TAG, token(repo))
    cfg, layers = m["config"], m["layers"]
    total = sum(x["size"] for x in layers) + cfg["size"]
    log(f"{site}: {repo}:{TAG} {len(layers)} layers, {total / 1e9:.2f} GB compressed -> {d}")
    fetch_blob(repo, cfg["digest"], cfg["size"], d / (cfg["digest"].split(":")[1] + ".json"), log)
    names = []
    with ThreadPoolExecutor(4) as ex:
        futs = []
        for x in layers:
            gz = "gzip" in x["mediaType"] or x["mediaType"].endswith("tar+gzip")
            name = x["digest"].split(":")[1] + (".tar.gz" if gz else ".tar")
            names.append(name)
            futs.append(ex.submit(fetch_blob, repo, x["digest"], x["size"], d / name, log))
        for f in futs:
            f.result()
    (d / "manifest.json").write_text(json.dumps([{"Config": cfg["digest"].split(":")[1] + ".json",
                                                  "RepoTags": [f"{repo}:{TAG}"], "Layers": names}]))
    log(f"{site}: verifying decompressed layers against the image config (as docker load does) ...")
    if not verify_site(d, log):
        raise RuntimeError(f"{site}: layer verification failed (delete {d} and re-run)")
    (d / "COMPLETE").write_text(json.dumps({"image": f"{repo}:{TAG}", "bytes": total, "time": time.ctime()}))
    log(f"{site}: done")
    return d


def main(argv):
    if argv[:1] == ["--verify"]:  # re-check finished images (decompressed layer digests)
        ok = True
        for s in argv[1:] or SITES:
            d = OUT / s
            r = (d / "COMPLETE").exists() and verify_site(d)
            print(f"{s}: {'OK' if r else 'NOT OK'}", flush=True)
            ok &= bool(r)
        return 0 if ok else 1
    sites = argv or list(SITES)
    bad = []
    for s in sites:
        try:
            fetch_site(s, log=lambda m: print(m, flush=True))
        except Exception as e:  # noqa
            print(f"{s}: FAILED {e}", flush=True)
            bad.append(s)
    print("all images complete" if not bad else f"incomplete: {bad} (re-run to resume)", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
