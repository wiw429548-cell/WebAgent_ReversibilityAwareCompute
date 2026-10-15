"""Environment preparation for run_all.py (no GPU needed for anything in here except VLLMServer).

* install_packages()  : staged, resolver-safe installation of the web-agent stack WITHOUT touching vLLM/torch
                        (the first server run died with pip `resolution-too-deep`: libwebarena -> text-generation ->
                        huggingface-hub<1.0 conflicts with the installed vLLM/transformers; browsergym-core pins
                        playwright==1.44 which pins greenlet/pyee). Pinned packages are installed with --no-deps,
                        everything else under a constraints file that freezes every installed distribution.
* check_chromium()    : distinguishes "playwright missing" / "Chromium binary missing" / "Chromium system libraries
                        missing" (lists the missing .so files, optionally provisions them without root from Debian/
                        Ubuntu packages via `apt-get download` + `dpkg -x`).
* resolve_webarena()  : local container deployment (docker or rootless podman) or a remote WebArena host
                        (WA_HOST / webarena.host); decides state probes and the reset mechanism; aborts EARLY with
                        actionable instructions when no WebArena can be reached.
* reset_sites()       : per-site container re-creation, or a remote full reset (BrowserGym's WA_FULL_RESET protocol).
* VLLMServer          : OpenAI-compatible vLLM server holding cross-project GPU locks for its whole lifetime.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import runtime

ROOT = Path(__file__).resolve().parents[1]
SITES = ("shopping", "shopping_admin", "reddit", "gitlab")
CONTAINER = {"shopping": "shopping", "shopping_admin": "shopping_admin", "reddit": "forum", "gitlab": "gitlab"}
IMAGES = {"shopping": ("shopping_final_0712", 67.6), "shopping_admin": ("shopping_admin_final_0719", 9.6),
          "reddit": ("postmill-populated-exposed-withimg", 53.4), "gitlab": ("gitlab-populated-final-port8023", 77.8)}
# default image source: ServiceNow WebArena-Verified (Docker Hub; same data + DB credentials, upstream fixes); sizes =
# loaded (uncompressed) GB. Pre-downloaded copies: downloads/docker_images/<site> (scripts/fetch_images.py)
VERIFIED_TAG = "0.1.0"
VERIFIED = {s: (f"am1n3e/webarena-verified-{s}:{VERIFIED_TAG}", gb)
            for s, gb in (("shopping", 13.3), ("shopping_admin", 2.9), ("reddit", 8.4), ("gitlab", 31.6))}


def image_table(source):
    return VERIFIED if source == "verified" else IMAGES


SITE_ENV = {"shopping": "WA_SHOPPING", "shopping_admin": "WA_SHOPPING_ADMIN", "reddit": "WA_REDDIT",
            "gitlab": "WA_GITLAB"}


class PreflightError(SystemExit):
    """raised (exit code 2) when the experiments cannot run on this machine; message = what to do"""

    def __init__(self, msg):
        super().__init__(2)
        self.msg = msg


def log(msg):
    print(f"[run_all] {msg}", flush=True)


def _run(cmd, timeout=60, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


# =========================================================================================== packages
# (import name, pip spec, install with --no-deps)
BASE_REQS = [
    ("yaml", "pyyaml", False), ("numpy", "numpy", False), ("sklearn", "scikit-learn", False),
    ("scipy", "scipy", False), ("matplotlib", "matplotlib", False), ("openai", "openai>=1.0", False),
    ("requests", "requests", False), ("tqdm", "tqdm", False), ("PIL", "pillow", False),
    ("gymnasium", "gymnasium>=0.27", False), ("bs4", "beautifulsoup4>=4.12", False), ("lxml", "lxml>=4.9,<6", False),
    ("pyparsing", "pyparsing>=3", False), ("nltk", "nltk", False), ("tiktoken", "tiktoken", False),
    ("aiolimiter", "aiolimiter", False), ("aiohttp", "aiohttp>=3.8,<4", False), ("pydantic", "pydantic>2,<3", False),
    # libwebarena pins beartype==0.12.0, which predates Python 3.12; any modern beartype works for its decorators
    ("beartype", "beartype>=0.18", False),
    ("greenlet", "greenlet>=3.0.3", False), ("pyee", "pyee==11.1.0", False),
    ("transformers", "transformers", False),
]
PINNED_REQS = [  # exact versions verified together; installed with --no-deps so pip never re-resolves vLLM & co.
    ("playwright", "playwright==1.44.0", True),
    ("browsergym.core", "browsergym-core==0.14.3", True),
    ("browsergym.webarena", "browsergym-webarena==0.14.3", True),
    ("webarena", "libwebarena==0.0.4", True),
    ("text_generation", "text-generation==0.7.0", True),
]
IMPORT_CHECK = """
import os
for k, v in {"WA_SHOPPING": ":7770", "WA_SHOPPING_ADMIN": ":7780/admin", "WA_REDDIT": ":9999", "WA_GITLAB": ":8023",
             "WA_WIKIPEDIA": ":8888", "WA_MAP": ":3000", "WA_HOMEPAGE": ":4399"}.items():
    os.environ.setdefault(k, "http://localhost" + v)
    os.environ.setdefault(k[3:], os.environ[k])  # libwebarena's env_config asserts SHOPPING, REDDIT, ... at import
os.environ.setdefault("OPENAI_API_KEY", "EMPTY")
import race.compat  # noqa: F401  (text_generation stub if the real package cannot be imported)
import gymnasium, playwright.sync_api, browsergym.core, browsergym.webarena  # noqa
from browsergym.core.action.highlevel import HighLevelActionSet  # noqa
from webarena.evaluation_harness.evaluators import evaluator_router  # noqa
import sklearn, scipy, matplotlib, yaml, openai, nltk  # noqa
print("IMPORT_OK")
"""


def _importable(mod):
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _constraints_file():
    """pin every installed distribution to its current version -> pip can only ADD packages"""
    from importlib.metadata import distributions
    pins = {}
    for d in distributions():
        try:
            name, ver = d.metadata["Name"], d.version
        except Exception:
            continue
        if name and ver:
            pins.setdefault(re.sub(r"[-_.]+", "-", name).lower(), ver)  # first on sys.path = the one imported
    p = runtime.LOCK_DIR / f"race_constraints_{os.getpid()}.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"{k}=={v}\n" for k, v in sorted(pins.items())))
    return p


WHEELS = ROOT / "downloads" / "wheels"  # pre-downloaded Linux cp312 wheels of the web-agent stack (offline install)


def _pip(args, tries=2):
    links = ["--find-links", str(WHEELS)] if WHEELS.is_dir() and any(WHEELS.glob("*.whl")) else []
    cmd = [sys.executable, "-m", "pip", "install", "--no-input", "--progress-bar", "off", *links, *args]
    for i in range(tries):
        log("$ " + " ".join(cmd))
        if subprocess.call(cmd) == 0:
            return True
        time.sleep(10)  # NFS 'Stale file handle' / network hiccups are transient
    return False


def install_packages():
    """Installs only what is missing; never upgrades anything. Returns True if the import check passes."""
    reqs = BASE_REQS + PINNED_REQS
    missing = [r for r in reqs if not _importable(r[0])]
    if missing and os.environ.get("WWW_NO_PIP"):
        log(f"missing {[m for m, _, _ in missing]} and WWW_NO_PIP is set -> not installing")
    elif missing:
        runtime.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lf = open(runtime.LOCK_DIR / "pip.lock", "a+")
        try:
            if runtime.fcntl is not None:
                log("waiting for the cross-project pip lock ...")
                runtime.fcntl.flock(lf.fileno(), runtime.fcntl.LOCK_EX)
            missing = [r for r in reqs if not _importable(r[0])]
            cons = _constraints_file()
            try:
                normal = [s for m, s, nd in missing if not nd]
                if normal and not _pip(["-c", str(cons), *normal]):
                    log("constrained install failed -> installing the remaining packages one by one")
                    for m, s, _ in missing:
                        if s in normal and not _importable(m):
                            if not _pip(["-c", str(cons), s], tries=1):
                                _pip(["--no-deps", s], tries=1)
                for m, s, nd in missing:
                    if nd and not _importable(m):
                        _pip(["--no-deps", s])
            finally:
                cons.unlink(missing_ok=True)
                import importlib
                importlib.invalidate_caches()
        finally:
            if runtime.fcntl is not None:
                runtime.fcntl.flock(lf.fileno(), runtime.fcntl.LOCK_UN)
            lf.close()
    still = [m for m, _, _ in reqs if not _importable(m)]
    if still:
        log(f"still missing after installation: {still}")
    r = _run([sys.executable, "-c", IMPORT_CHECK], timeout=600, cwd=str(ROOT))
    if "IMPORT_OK" not in r.stdout:
        log("import check of the web-agent stack failed:\n" + (r.stdout + r.stderr)[-3000:])
        return False
    try:  # punkt for webarena's must_include tokenisation (into NLTK_DATA = <project>/downloads/nltk_data)
        import nltk
        dd = os.environ.get("NLTK_DATA")
        if dd:
            Path(dd).mkdir(parents=True, exist_ok=True)
            if dd not in nltk.data.path:
                nltk.data.path.insert(0, dd)
        for res in ("punkt_tab", "punkt"):
            try:
                nltk.data.find(f"tokenizers/{res}")
            except LookupError:
                nltk.download(res, quiet=True, **({"download_dir": dd} if dd else {}))
    except Exception as e:  # noqa
        log(f"WARNING: nltk punkt download failed: {e}")
    return True


# =========================================================================================== Chromium
LAUNCH_PROBE = """
import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    if sys.argv[1] == "path":
        print("EXE=" + p.chromium.executable_path)
    else:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(); pg.set_content("<p>ok</p>"); assert pg.inner_text("p") == "ok"; b.close()
        print("LAUNCH_OK")
"""
# Ubuntu/Debian packages providing Chromium's runtime libraries (playwright nativeDeps, chromium)
DEB_PKGS = ["libnss3", "libnspr4", "libatk1.0-0", "libatk-bridge2.0-0", "libcups2", "libdrm2", "libxkbcommon0",
            "libxcomposite1", "libxdamage1", "libxfixes3", "libxrandr2", "libgbm1", "libpango-1.0-0",
            "libcairo2", "libasound2", "libatspi2.0-0", "libxshmfence1", "libwayland-server0", "libwayland-client0",
            "libxcb-dri3-0", "libdbus-1-3", "libexpat1", "libxi6", "libxtst6", "libxcursor1", "libxrender1",
            "libxext6", "libx11-6", "libxcb1", "libxau6", "libxdmcp6", "libbsd0", "libffi7", "libffi8",
            "libharfbuzz0b", "libfontconfig1", "libfreetype6", "libpng16-16", "libpixman-1-0", "libfribidi0",
            "libthai0", "libdatrie1", "libgraphite2-3", "libavahi-client3", "libavahi-common3", "libgnutls30",
            "libglib2.0-0", "libpangocairo-1.0-0", "libpangoft2-1.0-0", "libxcb-render0", "libxcb-shm0",
            "libgtk-3-0", "libgdk-pixbuf-2.0-0", "libgdk-pixbuf2.0-0", "libepoxy0", "libmd0", "libbrotli1",
            "libuuid1", "libxinerama1", "libwayland-cursor0", "libwayland-egl1", "libcairo-gobject2", "libdatrie1"]
LIBS_DIR = Path(os.environ.get("RACE_CHROMIUM_LIBS") or Path.home() / ".cache" / "race_chromium_libs")


def _lib_paths():
    out = []
    for sub in ("usr/lib/x86_64-linux-gnu", "lib/x86_64-linux-gnu", "usr/lib64", "usr/lib"):
        p = LIBS_DIR / "root" / sub
        if p.is_dir():
            out.append(str(p))
    return out


def chromium_env(env=None):
    """environment for processes that start Chromium (adds the no-root library dir if it was provisioned)"""
    env = dict(env or os.environ)
    lp = _lib_paths()
    if lp:
        env["LD_LIBRARY_PATH"] = ":".join(lp + [x for x in env.get("LD_LIBRARY_PATH", "").split(":") if x])
    return env


def _extract_deb(deb, root):
    """unpack the data member of a .deb (ar archive; data.tar.xz/gz/bz2) with the standard library -- no dpkg"""
    import io
    import tarfile
    with open(deb, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise ValueError(f"{deb} is not a .deb archive")
        while True:
            hdr = f.read(60)
            if len(hdr) < 60:
                raise ValueError(f"{deb}: no data.tar member")
            name, size = hdr[:16].decode().strip().rstrip("/"), int(hdr[48:58].decode().strip())
            data = f.read(size)
            if size % 2:
                f.read(1)
            if name.startswith("data.tar"):
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as t:
                    kw = {"filter": "tar"} if hasattr(tarfile, "data_filter") else {}
                    t.extractall(root, **kw)
                return


def _provision_libs_offline():
    """Chromium's libraries from the pre-downloaded Ubuntu 20.04 packages (scripts/fetch_chromium_libs.py)"""
    debs = sorted((LIBS_DIR / "debs").glob("*.deb"))
    if not debs or not sys.platform.startswith("linux"):
        return False, "no pre-downloaded packages"
    bad = []
    for d in debs:
        try:
            _extract_deb(d, LIBS_DIR / "root")
        except Exception as e:  # noqa
            bad.append(f"{d.name}: {e}")
    return len(bad) < len(debs), f"unpacked {len(debs) - len(bad)}/{len(debs)} packages into {LIBS_DIR / 'root'}" + \
        (f"; failed: {bad[:3]}" if bad else "")


def _provision_libs_nonroot():
    """apt-get download + dpkg -x of Chromium's runtime libraries into LIBS_DIR (Debian/Ubuntu only, no root)."""
    if not (shutil.which("apt-get") and shutil.which("dpkg")):
        return False, "apt-get/dpkg not available (not a Debian/Ubuntu system)"
    debs = LIBS_DIR / "debs"
    debs.mkdir(parents=True, exist_ok=True)
    got = 0
    for pkg in dict.fromkeys(DEB_PKGS):
        if any(debs.glob(f"{pkg}_*.deb")):
            got += 1
            continue
        r = _run(["apt-get", "download", pkg], timeout=300, cwd=str(debs))
        got += r.returncode == 0
    for deb in debs.glob("*.deb"):
        _run(["dpkg", "-x", str(deb), str(LIBS_DIR / "root")], timeout=300)
    return got > 0, f"downloaded/extracted {got} packages into {LIBS_DIR}"


PW_OFFLINE = ROOT / "downloads" / "playwright_linux"  # chromium-<rev>-chromium-linux.zip, ffmpeg-<rev>-ffmpeg-linux.zip


def _unzip_with_modes(zf, dest):
    import zipfile
    with zipfile.ZipFile(zf) as z:
        for info in z.infolist():
            out = z.extract(info, dest)
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                os.chmod(out, mode)


def _offline_chromium(exe):
    """install Playwright's Linux Chromium (and ffmpeg) from the pre-downloaded zips, no network. exe is the path
    Playwright expects: <browsers>/chromium-<rev>/chrome-linux/chrome"""
    p = Path(exe)
    m = re.match(r"chromium-(\d+)$", p.parents[1].name) if len(p.parents) > 1 else None
    z = PW_OFFLINE / f"chromium-{m.group(1)}-chromium-linux.zip" if m else None
    if not z or not z.exists() or not sys.platform.startswith("linux"):
        return False
    log(f"installing Chromium from the offline copy {z} -> {p.parents[1]}")
    try:
        _unzip_with_modes(z, p.parents[1])
        for f in ("INSTALLATION_COMPLETE", "DEPENDENCIES_VALIDATED"):
            (p.parents[1] / f).touch()
        for fz in PW_OFFLINE.glob("ffmpeg-*-ffmpeg-linux.zip"):
            d = p.parents[2] / f"ffmpeg-{fz.name.split('-')[1]}"
            _unzip_with_modes(fz, d)
            (d / "INSTALLATION_COMPLETE").touch()
    except Exception as e:  # noqa
        log(f"offline Chromium install failed: {e}")
        return False
    return p.exists()


def check_chromium(allow_provision=True):
    """-> environment dict to use for Chromium-launching processes; raises PreflightError with an exact diagnosis"""
    if not _importable("playwright"):
        raise PreflightError(
            "the Python package `playwright` is NOT installed (installation failed, see the pip output above). "
            "Fix: python3.12 -m pip install --user --no-deps playwright==1.44.0 greenlet==3.0.3 pyee==11.1.0 "
            "and re-run.")
    r = _run([sys.executable, "-c", LAUNCH_PROBE, "path"], timeout=120)
    exe = (re.findall(r"EXE=(.+)", r.stdout) or [""])[0].strip()
    if exe and not Path(exe).exists():
        _offline_chromium(exe)
    if not exe or not Path(exe).exists():
        for i in range(3):
            log(f"$ {sys.executable} -m playwright install chromium   (attempt {i + 1}; no --with-deps: no root)")
            if subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"]) == 0:
                break
            time.sleep(30)
        r = _run([sys.executable, "-c", LAUNCH_PROBE, "path"], timeout=120)
        exe = (re.findall(r"EXE=(.+)", r.stdout) or [""])[0].strip()
        if not exe or not Path(exe).exists():
            raise PreflightError(
                f"the Chromium browser binary for Playwright is missing ({exe or 'unknown path'}) and "
                f"`python -m playwright install chromium` failed (network? disk quota in ~/.cache/ms-playwright?). "
                f"Fix: run `{sys.executable} -m playwright install chromium` manually (or set PLAYWRIGHT_BROWSERS_PATH "
                f"to a writable directory) and re-run.")
    env = chromium_env()
    r = _run([sys.executable, "-c", LAUNCH_PROBE, "launch"], timeout=180, env=env)
    if "LAUNCH_OK" in r.stdout:
        log(f"headless Chromium OK ({exe})")
        return env

    def missing_libs(e):
        out = _run(["ldd", exe], timeout=60, env=e).stdout
        return sorted({ln.split("=>")[0].strip() for ln in out.splitlines() if "not found" in ln})

    miss = missing_libs(env)
    err = (r.stdout + r.stderr)[-1500:]
    if miss and allow_provision:
        log(f"Chromium cannot start: missing system libraries {miss}; trying a no-root install into {LIBS_DIR} ...")
        # 1) the pre-downloaded Ubuntu 20.04 packages (no network, no dpkg); 2) apt-get download (Debian/Ubuntu)
        for name, provision in (("offline packages", _provision_libs_offline), ("apt-get download", _provision_libs_nonroot)):
            ok, info = provision()
            log(f"no-root library provisioning ({name}): {info}")
            if not ok:
                continue
            env = chromium_env()
            r = _run([sys.executable, "-c", LAUNCH_PROBE, "launch"], timeout=180, env=env)
            if "LAUNCH_OK" in r.stdout:
                log(f"headless Chromium OK with LD_LIBRARY_PATH={env['LD_LIBRARY_PATH']}")
                return env
            miss = missing_libs(env)
            err = (r.stdout + r.stderr)[-1500:]
            if not miss:
                break
    if miss:
        raise PreflightError(
            f"Playwright and its Chromium binary are installed, but Chromium cannot start because these SYSTEM "
            f"libraries are missing: {' '.join(miss)}.\n  Fix (needs an administrator once): "
            f"`sudo {sys.executable} -m playwright install-deps chromium`  (Debian/Ubuntu), or install the "
            f"packages providing these .so files; or put them in a directory and export RACE_CHROMIUM_LIBS=<dir> "
            f"(expects <dir>/root/usr/lib/x86_64-linux-gnu). Then re-run.")
    local = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if local and allow_provision and local.startswith(str(ROOT)):
        # the project disk may be mounted noexec: fall back to Playwright's default location (~/.cache/ms-playwright)
        log(f"Chromium under {local} does not start ({(err.strip().splitlines() or ['?'])[-1]}); retrying with "
            f"Playwright's default browser location")
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH")
        return check_chromium(allow_provision=False)
    raise PreflightError(f"headless Chromium does not start although no library is missing. Error:\n{err}\n"
                         "Check that ~/.cache/ms-playwright is readable/executable (NFS noexec?) or set "
                         "PLAYWRIGHT_BROWSERS_PATH to a local directory and re-run.")


# =========================================================================================== WebArena
def http_ok(url, timeout=15):
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "race-preflight"}), timeout=timeout)
        return True
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def container_cli(pref="auto"):
    """'docker' | 'podman' | None (usable = `<cli> info` succeeds for this user)"""
    if pref in (None, "none", False):
        return None
    cands = ["docker", "podman"] if pref in ("auto", True) else [pref]
    for c in cands:
        if shutil.which(c) and _run([c, "info"], timeout=60).returncode == 0:
            return c
    return None


def _cli_problem():
    msgs = []
    for c in ("docker", "podman"):
        if not shutil.which(c):
            msgs.append(f"`{c}` not installed")
        else:
            r = _run([c, "info"], timeout=60)
            msgs.append(f"`{c} info` failed: {(r.stderr or r.stdout).strip().splitlines()[-1:] or ['?']}")
    return "; ".join(msgs)


def _is_local(hostname):
    names = {"localhost", "127.0.0.1", "::1", socket.gethostname(), socket.getfqdn()}
    return hostname in names


def site_urls(cfg):
    wc = cfg["webarena"]
    host = os.environ.get("WA_HOST") or wc.get("host") or "localhost"
    if "://" not in host:
        host = "http://" + host
    hostname = urllib.parse.urlparse(host).hostname
    base = f"{urllib.parse.urlparse(host).scheme}://{hostname}"
    urls = {k: os.environ.get(k) or base + v for k, v in wc["urls"].items()}
    return hostname, urls


def resolve_webarena(cfg, skip_setup=False, results=None):
    """-> info dict (urls, cli, probes, reset mode). Exports WA_* variables. Raises PreflightError early."""
    wc = cfg["webarena"]
    hostname, urls = site_urls(cfg)
    for k, v in urls.items():
        os.environ[k] = v
    os.environ["WA_HOSTNAME"] = hostname
    local = _is_local(hostname)
    pref = wc.get("container_cli", "auto")
    if wc.get("docker") is False:  # legacy key
        pref = "none"
    cli = container_cli(pref) if local else None
    source = os.environ.setdefault("WA_IMAGE_SOURCE", wc.get("image_source", "verified"))  # used by scripts/*.sh
    os.environ.setdefault("WA_VERIFIED_TAG", VERIFIED_TAG)
    os.environ.setdefault("PYTHON", sys.executable)
    targets = {s: urls[SITE_ENV[s]] for s in SITES}
    up = {s: http_ok(u) for s, u in targets.items()}
    log(f"WebArena host {hostname} ({'local' if local else 'remote'}); container CLI: {cli or 'none'}; "
        f"reachable: {up}")
    if not all(up.values()):
        down = [s for s, ok in up.items() if not ok]
        if not local:
            raise PreflightError(
                f"the remote WebArena host {hostname} is not reachable for sites {down} "
                f"({', '.join(targets[s] for s in down)}). Fix: start the sites on that host (standard WebArena ports "
                f"7770, 7780, 9999, 8023) or correct WA_HOST / the WA_* URLs, then re-run.")
        if cli is None:
            raise PreflightError(
                "WebArena is not running on this machine and no usable container runtime was found "
                f"({_cli_problem()}).\n  RACE needs the four self-hosted WebArena sites (shopping, shopping_admin, "
                "reddit, gitlab). Either\n  (a) get Docker (user in the `docker` group) or rootless Podman on this "
                "server, with ~60 GB free disk in the container storage for the WebArena-Verified images (default; ~210 GB + "
                "~80 GB download space with webarena.image_source: original); for rootless podman put graphroot on a "
                "LOCAL disk in ~/.config/containers/storage.conf, "
                "not NFS) and >=32 GB free RAM; run_all.py then deploys the sites itself; or\n  (b) run WebArena on "
                "another machine (e.g. with scripts/setup_webarena.sh there) and start this run with "
                "`WA_HOST=<that-host> [WA_FULL_RESET=http://<that-host>:7565] python run_all.py` (resets: BrowserGym's "
                "WA_FULL_RESET protocol).\n  Nothing was run and no GPU was used.")
        if skip_setup:
            raise PreflightError(f"WebArena sites {down} are not reachable and --skip_setup was given.")
        missing_gb = sum(gb for s, (img, gb) in image_table(source).items()
                         if _run([cli, "image", "inspect", img], timeout=60).returncode != 0)
        if missing_gb:
            _check_container_storage(cli, missing_gb * 1.05)
            if source != "verified":
                _check_disk()
        env = dict(os.environ, WA_CLI=cli)
        log(f"deploying WebArena with {cli} (one-time) from " + (
            f"WebArena-Verified {VERIFIED_TAG} images (pre-downloaded copies in downloads/docker_images are loaded, "
            f"missing ones are pulled from Docker Hub) ..." if source == "verified" else
            "the original Google Drive tarballs (~208 GB; daily download quota per file: the download waits and "
            "resumes automatically) ..."))
        if subprocess.call(["bash", str(ROOT / "scripts" / "setup_webarena.sh")], env=env, cwd=str(ROOT)) != 0:
            raise PreflightError(f"WebArena deployment with {cli} failed (see the output above: disk space? image "
                                 f"download? port already in use?). Fix the problem and re-run (downloads resume).")
        up = {s: http_ok(u) for s, u in targets.items()}
        if not all(up.values()):
            raise PreflightError(f"WebArena sites still unreachable after deployment: {up}")
    containers = {}
    if cli:
        for s in SITES:
            containers[s] = _run([cli, "inspect", CONTAINER[s]], timeout=60).returncode == 0
    probes = sorted(s for s, ok in containers.items() if ok)
    mode = wc.get("reset", "auto")
    if mode == "auto":
        if cli and all(containers.get(s) for s in SITES):
            mode = "container"
        elif os.environ.get("WA_FULL_RESET"):
            mode = "remote"
        else:
            mode = "none"
    if mode == "none" and not wc.get("allow_no_reset", False):
        raise PreflightError(
            "WebArena is reachable but its sites cannot be reset (no local containers named shopping/shopping_admin/"
            "forum/gitlab and no WA_FULL_RESET endpoint). Without resets the state changes of one method leak into the "
            "next one and the comparison is invalid. Fix: provide WA_FULL_RESET=<reset server url> (BrowserGym "
            "protocol: GET /reset, GET /status -> 'Ready for duty!'), or run the sites locally with docker/podman, "
            "or (not recommended) set webarena.allow_no_reset: true in configs/default.yaml.")
    probe_check = None
    if probes:  # validate the DB side-effect probes now (sub-queries that error in this image are dropped, logged)
        from .env import StateProbe
        sp = StateProbe(probes, cli)
        sp.snapshot(probes)
        probe_check = sp.describe()
        probes = [s for s in probes if s not in sp.broken]
        log(f"DB probes: {probe_check['active']} sub-queries active; dropped {probe_check['dropped'] or 'none'}; "
            f"unusable {probe_check['broken'] or 'none'}")
    info = {"hostname": hostname, "local": local, "urls": urls, "cli": cli, "containers": containers,
            "image_source": source if local else "remote",
            "probe_sites": probes, "probe_check": probe_check, "reset_mode": mode,
            "label_source": "db" if probes else "network", "full_reset_url": os.environ.get("WA_FULL_RESET")}
    if results is not None:
        Path(results).mkdir(parents=True, exist_ok=True)
        (Path(results) / "env_info.json").write_text(json.dumps(info, indent=2))
    log(f"WebArena ready: reset mode = {mode}; DB side-effect probes on {probes or 'no site'} "
        f"(others: network-request heuristic)")
    return info


def _container_storage(cli):
    """(runtime name, storage root, free GB, filesystem type) of the container runtime behind `cli` (docker may be a
    podman shim). Values are None when unknown."""
    ver = _run([cli, "--version"], timeout=60).stdout.lower()
    kind = "podman" if "podman" in ver else cli
    info = _run([cli, "info"], timeout=120).stdout
    m = re.search(r"(?:Docker Root Dir|graphRoot)\s*:\s*(\S+)", info)
    root = m.group(1).strip('"') if m else None
    free = fstype = None
    if root:
        try:
            free = shutil.disk_usage(root).free / 2 ** 30
        except OSError:
            pass
        fstype = (_run(["stat", "-f", "-c", "%T", root], timeout=30).stdout.strip() or None)
    return kind, root, free, fstype


def _check_container_storage(cli, needed_gb):
    kind, root, free, fstype = _container_storage(cli)
    log(f"container runtime: {kind} (command `{cli}`); image storage {root or '?'} "
        f"({'?' if free is None else f'{free:.0f} GB free'}, filesystem {fstype or '?'})")
    podman_fix = ("put podman's graphroot on a LOCAL disk with enough space (~/.config/containers/storage.conf, see "
                  "04_WebAgent_ReversibilityAwareCompute/RUN.md, section 'podman storage'), run `podman system reset` "
                  "once")
    if kind == "podman" and fstype and any(x in fstype.lower() for x in ("nfs", "lustre", "gpfs", "cifs", "smb")):
        raise PreflightError(
            f"rootless podman keeps its images in {root}, which is on a network filesystem ({fstype}); container "
            f"storage does not work there. Fix: {podman_fix}, and re-run. Nothing was downloaded.")
    if free is not None and free < needed_gb:
        fix = podman_fix if kind == "podman" else "ask the administrator for space (or a different docker data-root)"
        raise PreflightError(
            f"the container runtime ({kind}) stores images in {root}, which has only {free:.0f} GB free; the WebArena "
            f"images need ~{needed_gb:.0f} GB there. Fix: {fix}, and re-run. Nothing was downloaded.")
    if kind == "podman":
        _check_podman_rootless(cli)


def _check_podman_rootless(cli):
    """rootless podman: (1) the images contain files owned by many UIDs/GIDs (gitlab, mysql, ...) -> the user needs a
    subordinate id range (/etc/subuid, /etc/subgid) of >= 65536 ids, otherwise `podman load` fails AFTER the download;
    (2) without lingering, a systemd setting can kill the containers when the user logs out (warning only)."""
    r = _run([cli, "info", "--format", "{{json .Host.IDMappings}}"], timeout=120)
    try:
        m = json.loads(r.stdout or "{}")
    except ValueError:
        m = {}
    sizes = {k: sum(int(x.get("size", 0)) for x in (m.get(k) or [])) for k in ("uidmap", "gidmap")}
    log(f"rootless podman id mappings: {sizes.get('uidmap') or '?'} uids / {sizes.get('gidmap') or '?'} gids")
    if m and min(sizes.values()) < 65536:
        user = os.environ.get("USER") or "<user>"
        raise PreflightError(
            f"rootless podman has only {sizes} user/group ids for user namespaces; the WebArena images need >= 65536 "
            f"(subordinate id ranges). Fix (administrator, once): `sudo usermod --add-subuids 100000-165535 "
            f"--add-subgids 100000-165535 {user}` (or add `{user}:100000:65536` to /etc/subuid and /etc/subgid), then "
            f"`podman system migrate` and re-run. Nothing was downloaded.")
    lg = _run(["loginctl", "show-user", os.environ.get("USER") or "", "--property=Linger"], timeout=30).stdout.strip()
    if lg.endswith("=no"):
        log("WARNING: lingering is off for this user (`loginctl show-user $USER -p Linger` -> no). If the system kills "
            "user processes at logout, the WebArena containers stop when you log out. Run `loginctl enable-linger "
            "$USER` once (or ask the administrator), or keep a session open (tmux/screen) while RACE runs.")


def _check_disk():
    """tarballs are loaded one at a time and deleted afterwards (setup_webarena.sh): the download directory needs room
    for the largest one (gitlab, 78 GB); the container storage needs ~210 GB for the four loaded images."""
    d = Path(os.environ.get("WA_IMG_DIR") or ROOT / "webarena_images")
    d.mkdir(parents=True, exist_ok=True)
    have = sum(f.stat().st_size for f in d.glob("*.tar")) / 2 ** 30
    free = shutil.disk_usage(d).free / 2 ** 30
    if free + have < 80:
        raise PreflightError(f"not enough disk space for the WebArena image tarballs in {d}: {free:.0f} GB free "
                             f"(+{have:.0f} GB already downloaded), ~80 GB needed (the largest tarball; each one is "
                             f"deleted after loading), plus ~210 GB in the container storage (docker/podman root). "
                             f"Export WA_IMG_DIR=<directory on a big disk> and re-run.")
    log(f"disk for WebArena images: {free:.0f} GB free in {d} ({have:.0f} GB already downloaded); the container "
        f"storage needs ~210 GB for the four loaded images")


def reset_sites(info, sites, logf=log):
    """restore the given sites to their initial state. Returns True on success."""
    mode = info["reset_mode"]
    if mode == "container":
        env = dict(os.environ, WA_CLI=info["cli"])
        logf(f"resetting {list(sites)} ({info['cli']} re-create) ...")
        rc = subprocess.call(["bash", str(ROOT / "scripts" / "reset_webarena.sh"), *sites], env=env, cwd=str(ROOT),
                             stdout=sys.stdout, stderr=subprocess.STDOUT)
        return rc == 0
    if mode == "remote":
        url = info["full_reset_url"].rstrip("/")
        logf(f"remote full reset via {url}/reset ...")
        for attempt in range(3):
            try:
                urllib.request.urlopen(f"{url}/reset", timeout=60).read()
                t0 = time.time()
                while time.time() - t0 < 3600:
                    time.sleep(30)
                    try:
                        st = urllib.request.urlopen(f"{url}/status", timeout=30).read().decode()
                    except Exception:
                        continue
                    if "Ready for duty" in st:
                        return True
            except Exception as e:  # noqa
                logf(f"remote reset attempt {attempt + 1} failed: {e}")
                time.sleep(60)
        return False
    return True  # mode none: documented, nothing to do


# =========================================================================================== model weights
def model_dir(model):
    """plain-directory copy of a Hugging Face model inside the project (filled by run_all.download_models)"""
    return ROOT / "downloads" / "models" / model.replace("/", "--")


def model_complete(d):
    """config + tokenizer + every weight shard listed in the safetensors index are present (non-empty)"""
    d = Path(d)
    if not (d / "config.json").exists() or not any(d.glob("tokenizer*")):
        return False
    idx = d / "model.safetensors.index.json"
    if idx.exists():
        try:
            shards = set(json.loads(idx.read_text())["weight_map"].values())
        except Exception:  # noqa
            return False
        return all((d / s).exists() and (d / s).stat().st_size > 0 for s in shards)
    return any(f.stat().st_size > 0 for f in d.glob("*.safetensors"))


def local_model_dir(model):
    d = model_dir(model)
    return d if model_complete(d) else None


# =========================================================================================== vLLM server
VLLM_HINTS = [
    (r"CUDA out of memory|OutOfMemoryError|No available memory for the cache blocks|KV cache.*(?:larger|not enough)",
     "GPU memory: another process uses the GPUs, or max_model_len / gpu_mem too large (the next attempts reduce them)"),
    (r"not enough free GPU memory|Free memory on device.*less than desired",
     "GPU memory already in use by another process (check nvidia-smi; lower llm.gpu_mem)"),
    (r"Repository Not Found|404 Client Error|LocalEntryNotFoundError|does not appear to have a file|"
     r"OSError: .*(?:config\.json|safetensors)", "model weights missing / not downloadable (HF_HOME, HF_ENDPOINT, network)"),
    (r"Address already in use", "port already in use (another server); a free port is chosen on the next attempt"),
    (r"No CUDA GPUs are available|CUDA_VISIBLE_DEVICES|cudaErrorNoDevice|NVIDIA driver",
     "no usable GPU (CUDA_VISIBLE_DEVICES / driver)"),
    (r"nvcc|ninja|flashinfer|Triton.*compil|undefined symbol",
     "kernel compilation / binary mismatch (no nvcc on this machine; check the vllm / torch build)"),
    (r"ModuleNotFoundError|ImportError", "broken Python environment for vllm (import error above)"),
    (r"NCCL|ProcessGroup|RayWorker|Ray ", "multi-GPU communication (NCCL) problem with tensor parallelism"),
    (r"unrecognized arguments|error: argument", "a command-line flag is not supported by this vLLM version"),
]


def _log_tail(path, marker, n=60):
    """last n lines of the server log written after the last `marker` (the current attempt)"""
    try:
        txt = Path(path).read_text(errors="replace")
    except Exception:  # noqa
        return ""
    k = txt.rfind(marker)
    lines = (txt[k:] if k >= 0 else txt).splitlines()
    return "\n".join(lines[-n:])


def _vllm_hint(tail):
    for pat, hint in VLLM_HINTS:
        if re.search(pat, tail, re.I):
            return hint
    return ""


class VLLMServer:
    """vLLM OpenAI-compatible server over the visible GPUs. Holds cross-project GPU locks while alive."""

    def __init__(self, cfg, log_dir, engine_log):
        self.cfg, self.log_dir, self.engine_log = cfg, Path(log_dir), Path(engine_log)
        self.proc, self.model, self.info, self.fds = None, None, None, None
        self.restarts = 0
        self.gpus = runtime.visible_physical_gpus()
        # GiB kept free on every GPU below the measured free memory. vLLM applies gpu_mem to the (smaller) torch total
        # after its own CUDA context exists, and allocates some buffers (Marlin workspaces, NCCL) outside its budget:
        # Oct 15 a 2% margin left 40 MB free next to a foreign 18 GB process -> OOM on the first long prefill.
        self.headroom_gb = float(os.environ.get("RACE_VLLM_HEADROOM_GB", "3"))

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def _free_port(self, start):
        for p in range(start, start + 50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", p)) != 0:
                    return p
        raise RuntimeError("no free port for the vLLM server")

    def _ladder(self, model):
        lc = self.cfg["llm"]
        n = max(1, len(self.gpus))
        tp = min(int(lc.get("tp") or n), n)
        mml, gm = int(lc["max_model_len"]), float(lc["gpu_mem"])
        mem = runtime.gpu_memory()
        fr = [(mem[g][1] - mem[g][0] - 1024 * self.headroom_gb) / mem[g][1]
              for g in self.gpus[:tp] if g in mem and mem[g][1] > 0]
        ff = min(fr) if fr else None
        hi = gm if ff is None else max(0.4, min(0.93, round(ff, 3)))
        g0 = min(gm, hi)
        if tp >= 2 or "32B" not in model:
            lad = [dict(mml=mml, gm=g0, eager=False), dict(mml=mml, gm=hi, eager=True),
                   dict(mml=max(8192, mml // 2), gm=hi, eager=True)]
        else:  # 32B-AWQ on ONE 24 GB GPU: ~18 GB weights leave KV room for ~10k tokens only
            lad = [dict(mml=min(mml, 12288), gm=hi, eager=True), dict(mml=8192, gm=hi, eager=True)]
        return tp, lad

    def _wait_for_memory(self, model, tp):
        """block while other users' processes leave too little free memory for the model (shared GPUs, e.g. a foreign
        37 GB training job per GPU on cuda06, Oct 15): starting anyway only produces OOM attempts and stops the run"""
        need = (runtime.model_gpu_gb(model, tp) or 12.0) + 6.0 + self.headroom_gb  # weights + min. KV + headroom
        wait_h = float(os.environ.get("RACE_GPU_WAIT_H", "48"))
        t0, last = time.time(), 0.0
        while True:
            mem = runtime.gpu_memory()
            free = [(mem[g][1] - mem[g][0]) / 1024 for g in self.gpus[:tp] if g in mem]
            if not free or min(free) >= need:
                if last:
                    log(f"GPU memory available after {(time.time() - t0) / 3600:.1f} h: {min(free):.1f} GiB free")
                return
            if time.time() - t0 > wait_h * 3600:
                log(f"GPU memory still short after {wait_h:.0f} h (RACE_GPU_WAIT_H) -> trying anyway")
                return
            if time.time() - last >= 1800:
                log(f"GPUs {self.gpus[:tp]}: only {min(free):.1f} GiB free, vLLM {model} needs ~{need:.0f} GiB per "
                    f"GPU -> another process uses the GPUs (nvidia-smi); waiting (checked every minute, up to "
                    f"{wait_h:.0f} h)")
                last = time.time()
            time.sleep(60)

    def start(self, model):
        if self.fds is None:
            self.fds = runtime.lock_gpus_blocking(self.gpus, tag=f"04-race vllm {model}", log=log)
        self.model = model
        for rnd in range(3):
            try:
                return self._start(model)
            except RuntimeError as e:
                if rnd == 2 or "GPU memory" not in str(e):
                    raise
                log(f"vLLM could not start for lack of GPU memory (round {rnd + 1}/3) -> waiting 10 min and retrying")
                time.sleep(600)

    def _start(self, model):
        tp = min(int(self.cfg["llm"].get("tp") or max(1, len(self.gpus))), max(1, len(self.gpus)))
        self._wait_for_memory(model, tp)
        tp, lad = self._ladder(model)
        errors = []
        for i, a in enumerate(lad):
            self.port = self._free_port(int(self.cfg["llm"].get("port", 8000)))
            src = local_model_dir(model) or model  # project copy of the weights if complete, else the Hub name
            cmd = [sys.executable, "-m", "vllm.entrypoints.openai.api_server", "--model", str(src),
                   "--served-model-name", model, "--host", "127.0.0.1", "--port", str(self.port),
                   "--tensor-parallel-size", str(tp), "--max-model-len", str(a["mml"]),
                   "--gpu-memory-utilization", str(a["gm"]), "--enable-prefix-caching", "--seed",
                   str(self.cfg["seed"])] + (["--enforce-eager"] if a["eager"] else [])
            runtime.setup_env()  # VLLM_USE_FLASHINFER_SAMPLER=0 etc. (no nvcc on the server)
            env = dict(os.environ)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            lf = open(self.log_dir / "vllm_server.log", "a")
            lf.write(f"\n===== {time.ctime()} attempt {i + 1}: {' '.join(cmd)}\n")
            lf.flush()
            log(f"starting vLLM (attempt {i + 1}/{len(lad)}): {model} tp={tp} max_model_len={a['mml']} "
                f"gpu_mem={a['gm']} eager={a['eager']} port={self.port}")
            self.proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            t0 = time.time()
            timeout_min = float(self.cfg["llm"].get("start_timeout_min", 40))
            while time.time() - t0 < timeout_min * 60:
                if self.proc.poll() is not None:
                    break
                if http_ok(self.base_url + "/models", timeout=5) and self._serves(model):
                    self.info = dict(model=model, tp_requested=len(self.gpus) or 1, tp=tp, attempt=i + 1,
                                     max_model_len=a["mml"], gpu_mem=a["gm"], enforce_eager=a["eager"],
                                     errors=errors, fallback=bool(i > 0 or a["mml"] != int(self.cfg["llm"]["max_model_len"])),
                                     argv="run_all.py vllm-server", time=time.ctime())
                    with open(self.engine_log, "a", encoding="utf-8") as f:
                        f.write(json.dumps(self.info) + "\n")
                    log(f"vLLM server up after {(time.time() - t0) / 60:.1f} min: {json.dumps(self.info)}")
                    os.environ["RACE_LLM_BASE_URL"] = self.base_url
                    os.environ["RACE_MAX_MODEL_LEN"] = str(a["mml"])
                    return self.info
                time.sleep(10)
            rc = self.proc.poll()
            why = (f"crashed with exit code {rc} after {(time.time() - t0) / 60:.1f} min" if rc is not None else
                   f"not ready after the start timeout of {timeout_min:.0f} min (llm.start_timeout_min) -> killed")
            tail = _log_tail(self.log_dir / "vllm_server.log", f"attempt {i + 1}: ")
            hint = _vllm_hint(tail)
            errors.append(f"attempt {i + 1} (max_model_len={a['mml']} gpu_mem={a['gm']} eager={a['eager']}): {why}"
                          + (f"; likely cause: {hint}" if hint else ""))
            log(f"vLLM attempt {i + 1}/{len(lad)} FAILED: {why}" + (f"\n  likely cause: {hint}" if hint else "")
                + "\n  last lines of logs/vllm_server.log:\n    " + "\n    ".join(tail.splitlines()[-40:]))
            self._kill()
            lf.close()
            time.sleep(20)
        raise RuntimeError(f"vLLM server for {model} could not start (all {len(lad)} attempts failed; full output in "
                           f"{self.log_dir / 'vllm_server.log'}):\n  - " + "\n  - ".join(errors))

    def _serves(self, model):
        try:
            return model in urllib.request.urlopen(self.base_url + "/models", timeout=10).read().decode()
        except Exception:
            return False

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and http_ok(self.base_url + "/models", timeout=20)

    def ensure_up(self):
        """restart a dead server (max 5 restarts per run_all invocation)"""
        if self.alive():
            return True
        if self.restarts >= 5:
            return False
        self.restarts += 1
        log(f"vLLM server is down -> restart #{self.restarts}")
        tail = _log_tail(self.log_dir / "vllm_server.log", "=====", n=400)
        if re.search(r"OOM on device|out of memory|OutOfMemoryError", tail, re.I):
            self.headroom_gb += 2.0
            log(f"  the server died of CUDA OOM while serving (another process shares the GPU?) -> GPU headroom "
                f"raised to {self.headroom_gb:.0f} GiB")
        self._kill()
        self.start(self.model)
        return True

    def _kill(self):
        p, self.proc = self.proc, None
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (OSError, AttributeError):
            p.terminate()
        try:
            p.wait(timeout=90)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (OSError, AttributeError):
                p.kill()
            p.wait(timeout=30)

    def stop(self, release=True):
        self._kill()
        if release and self.fds is not None:
            runtime.release_gpus(self.fds)
            self.fds = None
