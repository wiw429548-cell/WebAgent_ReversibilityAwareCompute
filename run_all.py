"""One-command entry point for ALL experiments of the RACE paper (unattended, resumable, GPU-lock aware).

    CUDA_VISIBLE_DEVICES=0,1 nohup python run_all.py > run.log 2>&1 &

0. preflight (no GPU is touched before all of these pass; failures exit with code 2 and say what to do):
   packages (staged, never re-resolves vLLM) -> headless Chromium -> WebArena (local docker/podman deployment or a
   remote host via WA_HOST; reset mechanism) -> model download. Then the vLLM server is started over the visible GPUs
   (holding the cross-project GPU locks until exit).
1. collect   : tryback_all on the template-disjoint WebArena collection split (labels for the predictor)
2. predictor : training + evaluation of the reversibility predictor (race.reversibility)
3. main      : main methods on WebArena-Lite (all non-map tasks); the compute-matched uniform baseline runs last,
               calibrated on RACE's measured tokens/step (results/compute_match.json)
4. ablation  : ablations on the stratified half ("ablation" split)
5. backbone  : extra backbone(s) (server switched to that model, then back)
6. sensitivity / variance on the ablation split
7. rejudge   : deferred LLM-judge calls of the extra-backbone episodes, with the judge model
8. aggregate : results/summary.md, results/latex/*.tex, results/figures/*
Every episode is saved as its own JSON file: a crash/kill after N tasks never redoes them. Optional stages (4-6) are
skipped if they would exceed the compute budget (--budget_hours, default configs: budget_hours); see
results/skipped_stages.json. WebArena flakiness: a runner exits 76 when a site stops answering or an episode exceeds
agent.episode_timeout_min; the lane is then retried after re-creating its site containers (container mode) or after a
5-min wait (remote mode); unfinished tasks are resumed, never double-counted.
Exit codes: 0 finished, 1 some stage incomplete (re-run to resume), 2 preflight failed (nothing ran).
Options: --config --skip_setup --max_tasks N (debug: results_debug/) --budget_hours H --preflight_only --no_install
         --download_only (fetch packages / Chromium / NLTK / weights / WebArena tarballs into this folder; no GPU)
All downloads go into this folder: downloads/{hf_home,ms-playwright,nltk_data,chromium_libs} and webarena_images/.
"""
import argparse
import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OPENAI_API_KEY", "EMPTY")
# every download lands inside the project folder (the home directory is a small NFS quota): model weights, the
# Chromium build of Playwright, its no-root system libraries, NLTK data and the WebArena image tarballs. Variables
# that are already set in the environment win.
DOWNLOADS = {"HF_HOME": ROOT / "downloads" / "hf_home", "PLAYWRIGHT_BROWSERS_PATH": ROOT / "downloads" / "ms-playwright",
             "NLTK_DATA": ROOT / "downloads" / "nltk_data", "RACE_CHROMIUM_LIBS": ROOT / "downloads" / "chromium_libs",
             "WA_IMG_DIR": ROOT / "webarena_images"}
for _k, _v in DOWNLOADS.items():
    os.environ.setdefault(_k, str(_v))
sys.path.insert(0, str(ROOT))

from race import infra, runtime  # noqa: E402

log = infra.log
CHILDREN = set()
CHILD_LOCK = threading.Lock()
STOP = threading.Event()


def sh(cmd, env=None, tag=""):
    """run a child (own process group, so that its Chromium processes can be killed with it) -> return code"""
    cmd = [str(c) for c in cmd]
    log(f"{tag}$ {' '.join(cmd)}")
    if STOP.is_set():
        return 130
    p = subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True)
    with CHILD_LOCK:
        CHILDREN.add(p)
    try:
        return p.wait()
    finally:
        with CHILD_LOCK:
            CHILDREN.discard(p)


def kill_children():
    with CHILD_LOCK:
        ps = list(CHILDREN)
    for p in ps:
        for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
            if p.poll() is not None:
                break
            try:
                os.killpg(p.pid, sig)
            except (OSError, AttributeError):
                p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass


class Orchestrator:
    def __init__(self, cfg, info, args, results):
        self.cfg, self.info, self.a, self.R = cfg, info, args, results
        self.server = infra.VLLMServer(cfg, ROOT / "logs", results / "engine_log.jsonl")
        self.site_lock = threading.Lock()
        # per host: every host runs its OWN WebArena containers (clean on cuda05 says nothing about cuda06)
        self.state_file = results / f"site_state_{socket.gethostname().split('.')[0]}.json"
        self.budget_file = results / "budget.json"
        self.budget_h = float(args.budget_hours or cfg.get("budget_hours") or 1e9)
        self.t_start = time.time()
        b = self._read(self.budget_file, {})
        self.used_before = float(b.get("wall_hours_used", 0.0))
        self.ep_wall = list(b.get("episode_wall_samples", []))
        self.failed = []
        self.browser_fails = 0
        self.models_ok = {}
        self.child_env = infra.chromium_env()
        self.child_env.update(RACE_PROBE_SITES=",".join(info["probe_sites"]), RACE_CONTAINER_CLI=info["cli"] or "")

    # ------------------------------------------------------------------ bookkeeping
    @staticmethod
    def _read(p, default):
        try:
            return json.loads(Path(p).read_text())
        except Exception:
            return default

    def used_hours(self):
        return self.used_before + (time.time() - self.t_start) / 3600

    def _save_budget(self):
        self.budget_file.parent.mkdir(parents=True, exist_ok=True)
        self.budget_file.write_text(json.dumps({"wall_hours_used": self.used_hours(), "budget_hours": self.budget_h,
                                                "episode_wall_samples": self.ep_wall[-200:]}, indent=1))

    def site_dirty(self, site):
        with self.site_lock:
            return self._read(self.state_file, {}).get(site, "dirty") != "clean"  # unknown = dirty

    def set_site(self, sites, state):
        with self.site_lock:
            st = self._read(self.state_file, {})
            st.update({s: state for s in sites})
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=1))
            os.replace(tmp, self.state_file)

    def ensure_clean(self, sites, tag):
        dirty = [s for s in sites if self.site_dirty(s)]
        if not dirty or self.info["reset_mode"] == "none":
            return True
        if self.info["reset_mode"] == "remote":
            dirty = list(infra.SITES)  # the remote protocol resets the whole instance
        for attempt in range(2):
            if infra.reset_sites(self.info, dirty, logf=lambda m: log(f"{tag}{m}")):
                self.set_site(dirty, "clean")
                return True
            log(f"{tag}reset of {dirty} failed (attempt {attempt + 1})")
        return False

    # ------------------------------------------------------------------ jobs
    def lanes_todo(self, method, split, model, seed):
        from race.env import task_tables
        from race.runner import heal_browser_failures, lane_of, out_dir, task_split
        _, full = task_tables()
        out = out_dir(split, method, model, seed)
        if out.exists() and heal_browser_failures(out):
            log(f"{out.relative_to(ROOT)}: episodes recorded while the browser could not start or hit a fixed harness "
                f"bug -> moved to *.json.browser_failed / *.json.harness_failed, they are run again")
        lanes = {}
        for i in task_split(self.cfg, split):
            lanes.setdefault(lane_of(full[i]["sites"]), []).append(i)
        if self.a.max_tasks:
            lanes = {k: v[:self.a.max_tasks] for k, v in lanes.items()}
        return {k: v for k, v in lanes.items() if any(not (out / f"{i}.json").exists() for i in v)}, full, out

    def n_todo(self, method, split, model=None, seed=None):
        lanes, _, out = self.lanes_todo(method, split, model, seed)
        return sum(sum(not (out / f"{i}.json").exists() for i in v) for v in lanes.values())

    def run_lane(self, lane, ids, full, method, split, model, seed, results):
        tag = f"[{split}/{method}{'@' + model.split('/')[-1] if model else ''}/{lane}] "
        sites = sorted({s for i in ids for s in full[i]["sites"]})
        cmd = [sys.executable, "-m", "race.runner", "--method", method, "--split", split, "--lane", lane]
        if self.a.config:
            cmd += ["--config", self.a.config]
        if self.a.max_tasks:
            cmd += ["--max_tasks", self.a.max_tasks]
        if model:
            cmd += ["--model", model]
        if seed is not None:
            cmd += ["--seed", seed]
        env = dict(self.child_env, RACE_SERVED_MODEL=self.server.model or "",
                   RACE_LLM_BASE_URL=os.environ.get("RACE_LLM_BASE_URL", ""),
                   RACE_MAX_MODEL_LEN=os.environ.get("RACE_MAX_MODEL_LEN", ""),
                   RACE_ENGINE_INFO=json.dumps(self.server.info or {}))
        for attempt in range(3):
            if STOP.is_set():
                results[lane] = "stopped"
                return
            if self.info["reset_mode"] == "container" and not self.ensure_clean(sites, tag):
                results[lane] = "reset_failed"
                return
            self.set_site(sites, "dirty")  # from now on the agent may change server state
            rc = sh(cmd, env=env, tag=tag)
            if rc == 0:
                results[lane] = "ok"
                return
            if rc == 75:
                results[lane] = "llm_down"
                return
            if rc == 77:  # the headless browser cannot start: retrying the lane only burns tasks
                results[lane] = "browser_down"
                return
            if rc == 2:  # configuration problem: retrying cannot help
                results[lane] = "config_error"
                return
            log(f"{tag}runner exit code {rc} (attempt {attempt + 1}/3)")
            if rc == 76:  # a site is down or an episode hung: container mode re-creates the (dirty) site next attempt
                log(f"{tag}WebArena site down / episode timeout -> "
                    + ("site containers are re-created" if self.info["reset_mode"] == "container"
                       else "waiting 5 min for the host to recover"))
                if self.info["reset_mode"] != "container":
                    for _ in range(30):
                        if STOP.is_set():
                            break
                        time.sleep(10)
        results[lane] = f"rc{rc}"

    def job(self, method, split, model=None, seed=None):
        """run all lanes of one (method, split, model, seed); single-site lanes in parallel, then multi-site tasks"""
        name = f"{split}/{method}{'@' + model.split('/')[-1] if model else ''}{'_seed' + str(seed) if seed else ''}"
        for rnd in range(4):
            lanes, full, out = self.lanes_todo(method, split, model, seed)
            if not lanes:
                return True
            n0 = len(list(out.glob("*.json")))
            t0 = time.time()
            log(f"job {name}: lanes {sorted(lanes)} ({sum(len(v) for v in lanes.values())} tasks in these lanes)")
            results = {}
            single = [k for k in lanes if k != "multi"]
            remote = self.info["reset_mode"] == "remote"  # whole-instance reset: only between lane groups
            if remote and single and not self.ensure_clean(list(infra.SITES), "[remote] "):
                results.update({k: "reset_failed" for k in single})
                single = []
            th = [threading.Thread(target=self.run_lane, args=(k, lanes[k], full, method, split, model, seed, results),
                                   daemon=True) for k in single]
            for t in th:
                t.start()
            for t in th:
                while t.is_alive():
                    t.join(timeout=5)
            if "multi" in lanes and not STOP.is_set():
                if remote and not self.ensure_clean(list(infra.SITES), "[remote] "):
                    results["multi"] = "reset_failed"
                else:
                    self.run_lane("multi", lanes["multi"], full, method, split, model, seed, results)
            n1 = len(list(out.glob("*.json")))
            if n1 > n0:
                self.ep_wall.append((time.time() - t0) / (n1 - n0))
            self._save_budget()
            if STOP.is_set():
                return False
            if any(v == "browser_down" for v in results.values()):
                self.browser_fails += 1
                if self.browser_fails >= 2:
                    raise RuntimeError("the headless browser (Playwright driver) cannot be started from the runner "
                                       "processes (see 'fresh-process browser check' in the lane logs) -> stopping; "
                                       "nothing was recorded for those tasks, fix and re-run to resume")
                log(f"job {name}: headless browser could not start -> waiting 60 s and retrying once")
                time.sleep(60)
                continue
            if any(v == "ok" for v in results.values()):
                self.browser_fails = 0
            if any(v == "llm_down" for v in results.values()):
                if not self.cfg["llm"].get("serve", True):
                    time.sleep(300)
                    continue
                if not self.server.ensure_up():
                    raise RuntimeError("vLLM server keeps dying (5 restarts) -> stopping; re-run to resume")
                continue
            if all(v == "ok" for v in results.values()):
                return True
            log(f"job {name}: lane results {results} (round {rnd + 1})")
        self.failed.append(name)
        return False

    # ------------------------------------------------------------------ budget
    def episode_hours(self):
        s = sorted(self.ep_wall)
        per = s[len(s) // 2] if len(s) >= 3 else float(self.cfg.get("est_min_per_episode", 1.5)) * 60
        return per / 3600

    def affordable(self, stage, jobs):
        n = sum(self.n_todo(*j) for j in jobs)
        est = n * self.episode_hours() * (1.0 if stage != "backbone" else 0.8) + (0.4 if stage == "backbone" else 0)
        ok = self.used_hours() + est <= self.budget_h
        log(f"budget: {self.used_hours():.1f} h used of {self.budget_h:.0f} h; stage {stage}: {n} episodes "
            f"~{est:.1f} h -> {'run' if ok else 'SKIP'}")
        if not ok:
            p = self.R / "skipped_stages.json"
            d = self._read(p, {})
            d[stage] = {"episodes_left": n, "estimated_hours": round(est, 2), "used_hours": round(self.used_hours(), 2),
                        "budget_hours": self.budget_h, "time": time.ctime()}
            p.write_text(json.dumps(d, indent=1))
        return ok

    # ------------------------------------------------------------------ server
    def serve(self, model):
        if not self.cfg["llm"].get("serve", True):  # external OpenAI-compatible endpoint (llm.base_url)
            if model != self.cfg["llm"]["model"]:
                raise RuntimeError(f"llm.serve=false: cannot switch the external endpoint to {model}")
            os.environ["RACE_SERVED_MODEL"] = model
            self.server.model = model
            return
        if self.server.model == model and self.server.alive():
            return
        if self.server.proc is not None:
            log(f"switching the served model {self.server.model} -> {model}")
            self.server.stop(release=False)  # keep the GPU locks
            time.sleep(15)
        self.server.start(model)
        os.environ["RACE_SERVED_MODEL"] = model

    # ------------------------------------------------------------------ stages
    def run(self):
        cfg, py = self.cfg, sys.executable
        c = ["--config", self.a.config] if self.a.config else []
        main_model = cfg["llm"]["model"]
        from race.reversibility import MODEL
        self.serve(main_model)
        # 1-2. collection + predictor
        if not self.job(cfg["collect"]["policy"], "collect"):
            raise RuntimeError("collection split incomplete -> cannot train the reversibility predictor yet")
        from race.runner import out_dir
        newest = max((p.stat().st_mtime for p in out_dir("collect", cfg["collect"]["policy"]).glob("*.json")),
                     default=0.0)
        if MODEL.exists() and MODEL.stat().st_mtime < newest:
            log(f"collection data is newer than {MODEL.name} -> the reversibility predictor is retrained")
        if not MODEL.exists() or MODEL.stat().st_mtime < newest:
            if sh([py, "-m", "race.reversibility", *c], env=dict(self.child_env)) != 0 or not MODEL.exists():
                raise RuntimeError("training the reversibility predictor failed (see the log above)")
        # 3. main (never skipped); the compute-matched uniform baseline is calibrated on the finished race run
        for m in cfg["main_methods"]:
            if cfg["methods"][m].get("uniform_m2"):
                from race.compute_match import PATH, calibrate
                if not PATH.exists() and self.n_todo("race", "eval"):  # p is frozen: never calibrate on a partial run
                    self._skip(f"eval/{m}", "the race run is incomplete; calibrated after it finished (re-run)")
                    self.failed.append(f"eval/{m}")
                    continue
                cm = calibrate(cfg)
                if cm is None:
                    self._skip(f"eval/{m}", "too few race episodes to calibrate the compute-matched baseline")
                    self.failed.append(f"eval/{m}")
                    continue
                log(f"compute match for {m}: M2 probability p={cm['m2_prob']} (race: {cm['tokens_per_step_ref']:.0f} "
                    f"tokens/step from {cm['n_episodes']} episodes; results/compute_match.json)")
            self.job(m, "eval")
        # 4. ablations
        jobs = [(m, "ablation") for m in cfg["ablation_methods"]]
        if self.affordable("ablation", jobs):
            for j in jobs:
                self.job(*j)
        # 5. extra backbones
        for bb in cfg.get("extra_backbones", []):
            jobs = [(m, bb.get("split", "ablation"), bb["model"]) for m in bb["methods"]]
            if not any(self.n_todo(*j) for j in jobs):
                continue
            if not self.models_ok.get(bb["model"], False):
                self._skip("backbone", f"model {bb['model']} could not be downloaded")
                continue
            if self.affordable("backbone", jobs):
                self.serve(bb["model"])
                for j in jobs:
                    self.job(*j)
        # 6. sensitivity + variance (back on the main model)
        for stage, jobs in (("sensitivity", [(m, "ablation") for m in cfg.get("sensitivity_methods", [])]),
                            ("variance", [(m, "ablation", None, cfg["variance_seed"])
                                          for m in cfg.get("variance_methods", [])])):
            if jobs and any(self.n_todo(*j) for j in jobs) and self.affordable(stage, jobs):
                self.serve(main_model)
                for j in jobs:
                    self.job(*j)
        # 7. deferred judge calls (episodes run while another model than the judge was served)
        from race.judge import judge_model
        from race.rejudge import pending_files
        if any(True for _ in pending_files(self.R)):
            self.serve(judge_model(cfg))
            if sh([py, "-m", "race.rejudge", *c], env=dict(self.child_env, RACE_SERVED_MODEL=self.server.model)) != 0:
                self.failed.append("rejudge")

    def _skip(self, stage, why):
        p = self.R / "skipped_stages.json"
        d = self._read(p, {})
        d[stage] = {"reason": why, "time": time.ctime()}
        p.write_text(json.dumps(d, indent=1))
        log(f"stage {stage} skipped: {why}")


def download_models(cfg):
    """pre-download the weights (CPU only) so that the GPUs are never held while downloading. Weights go to plain
    directories downloads/models/<org>--<name> (no cache symlinks: the folder can be copied between machines);
    vLLM is started from that directory (infra.local_model_dir) and serves it under the Hugging Face name."""
    from huggingface_hub import snapshot_download
    ok = {}
    models = [cfg["llm"]["model"]] + [bb["model"] for bb in cfg.get("extra_backbones", [])]
    for i, m in enumerate(dict.fromkeys(models)):
        d = infra.model_dir(m)
        if infra.model_complete(d):  # copied together with the project: no Hub access at all
            ok[m] = True
            log(f"model {m}: complete local copy in {d}")
            continue
        for attempt in range(4):
            try:
                snapshot_download(m, local_dir=str(d), allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model",
                                                                       "*.py", "tokenizer*", "*.tiktoken"])
                ok[m] = infra.model_complete(d)
                if ok[m]:
                    log(f"model {m} available locally in {d}")
                    break
                log(f"model {m}: files missing in {d} after download (attempt {attempt + 1}/4)")
            except Exception as e:  # noqa
                if infra.model_complete(d):  # offline / Hub unreachable, but the copied weights are complete
                    ok[m] = True
                    log(f"model {m}: Hub not reachable ({str(e)[:120]}), using the complete local copy in {d}")
                    break
                log(f"download of {m} failed (attempt {attempt + 1}/4): {str(e)[:300]}")
            time.sleep(60 * (attempt + 1))
        if not ok.get(m) and i == 0:
            raise infra.PreflightError(f"the main model {m} could not be downloaded from the Hugging Face Hub (see "
                                       f"above). Check the network / HF_ENDPOINT / disk quota of HF_HOME and re-run.")
    return ok


def download_only(cfg, a):
    """--download_only: everything that can be fetched without a GPU or a container runtime, into the project folder.
    The WebArena tarballs are kept (WA_KEEP_TARS=1); a later `python run_all.py` loads them and deletes them."""
    rc = 0
    try:
        if not a.no_install and not infra.install_packages():
            log("package installation incomplete (see above)")
            rc = 1
        try:
            infra.check_chromium()
        except infra.PreflightError as e:
            log(f"Chromium: {e.msg}")
            rc = 1
        ok = download_models(cfg)
        log(f"model weights: {ok}")
    except infra.PreflightError as e:
        log(e.msg)
        return 2
    env = dict(os.environ, WA_DOWNLOAD_ONLY="1", WA_KEEP_TARS="1", PYTHON=sys.executable,
               WA_IMAGE_SOURCE=os.environ.get("WA_IMAGE_SOURCE") or cfg["webarena"].get("image_source", "verified"))
    if sh(["bash", ROOT / "scripts" / "setup_webarena.sh"], env=env, tag="[download] ") != 0:
        log("WebArena image download incomplete (Google Drive quota?) -> re-run `python run_all.py --download_only` "
            "later; downloads resume")
        rc = 1
    log(f"download_only finished (exit {rc}); everything is under {ROOT}")
    return rc


LOCK_STALE_SEC = 300  # an owner file not touched for this long belongs to a dead run_all (heartbeat every 30 s)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def instance_lock(d, host):
    """cross-host single-instance lock -> None if acquired (released at exit), else a description of the holder"""
    me = f"pid={os.getpid()} host={host} started={time.ctime()}"
    owner = d / "owner"
    for _ in range(3):
        try:
            d.mkdir()
        except FileExistsError:
            try:
                who = owner.read_text().strip() if owner.exists() else "starting"
                age = time.time() - (owner if owner.exists() else d).stat().st_mtime
            except FileNotFoundError:  # released right now -> retry
                continue
            kv = dict(x.split("=", 1) for x in who.split() if "=" in x)
            alive = age < LOCK_STALE_SEC
            if alive and kv.get("host") == host and kv.get("pid", "").isdigit():
                alive = _pid_alive(int(kv["pid"]))  # same host: the pid is authoritative
            if alive:
                return f"{who}, heartbeat {age:.0f}s ago"
            stale = d.with_name(f"{d.name}.stale-{os.getpid()}-{int(time.time())}")
            try:
                d.rename(stale)  # atomic: only one of several racing instances takes over
                log(f"removed a stale run_all lock ({who}, heartbeat {age:.0f}s ago)")
                shutil.rmtree(stale, ignore_errors=True)
            except OSError:
                pass
            continue
        owner.write_text(me + "\n")
        done = threading.Event()

        def heartbeat():
            while not done.wait(30):
                try:
                    os.utime(owner)
                except OSError as e:
                    log(f"run_all lock heartbeat failed: {e}")

        def release():
            done.set()
            try:
                if owner.read_text().strip() == me:
                    owner.unlink()
                    d.rmdir()
            except OSError:
                pass

        threading.Thread(target=heartbeat, daemon=True, name="run_all-lock").start()
        atexit.register(release)
        return None
    return "unknown (lock contention, retry)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--skip_setup", action="store_true", help="never deploy WebArena (it must be reachable)")
    ap.add_argument("--max_tasks", type=int, default=None, help="debug: N tasks per lane, results_debug/")
    ap.add_argument("--budget_hours", type=float, default=None)
    ap.add_argument("--preflight_only", action="store_true")
    ap.add_argument("--no_install", action="store_true")
    ap.add_argument("--download_only", action="store_true",
                    help="only download everything into the project folder (packages, Chromium, NLTK data, model "
                         "weights, WebArena image tarballs); no GPU, no docker needed")
    a = ap.parse_args()
    if a.max_tasks:
        os.environ["RACE_DEBUG"] = "1"
    runtime.setup_env()
    (ROOT / "logs").mkdir(exist_ok=True)
    from race.utils import RESULTS, load_config
    cfg = load_config(a.config)
    t0 = time.time()
    import socket
    log(f"start {time.ctime()}  host {socket.gethostname()}  python {sys.executable}  "
        f"CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES')}")
    log("download locations: " + ", ".join(f"{k}={os.environ[k]}" for k in DOWNLOADS))
    if a.download_only:
        return download_only(cfg, a)
    # one run_all per results directory, across ALL hosts sharing this NFS volume (two instances race on the result
    # files and corrupt the sqlite LLM caches). flock is host-local on this NFS mount (local_lock=all), so the lock is an
    # mkdir (atomic on the NFS server) + an owner file whose mtime is a heartbeat.
    RESULTS.mkdir(parents=True, exist_ok=True)
    holder = instance_lock(RESULTS / ".run_all.lockd", socket.gethostname())
    if holder:
        log(f"another run_all is already running on {RESULTS} ({holder}) -> exit 2. If that is wrong (it crashed on "
            f"another host), wait {LOCK_STALE_SEC}s for its heartbeat to expire or remove {RESULTS / '.run_all.lockd'}")
        return 2
    orch = None

    def on_signal(sig, frame):
        log(f"signal {sig} received -> stopping children, the vLLM server and releasing the GPU locks")
        STOP.set()
        raise SystemExit(128 + sig)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # nohup semantics even without nohup
    rc = 0
    try:
        try:
            if not a.no_install and not infra.install_packages():
                raise infra.PreflightError("the web-agent Python stack (playwright / browsergym / webarena) cannot be "
                                           "imported after installation; see the import error above.")
            infra.check_chromium()
            import importlib.util
            if cfg["llm"].get("serve", True) and importlib.util.find_spec("vllm") is None:
                raise infra.PreflightError("vLLM is not installed for this Python (it is expected to be pre-installed; "
                                           "run_all never installs it). Install vllm and re-run.")
            info = infra.resolve_webarena(cfg, a.skip_setup, results=RESULTS)
            if a.preflight_only:
                log("preflight OK (--preflight_only): nothing else is run")
                return 0
            models_ok = download_models(cfg)
        except infra.PreflightError as e:
            log("PREFLIGHT FAILED -- nothing was run, no GPU was used.\n" + "=" * 100 + f"\n{e.msg}\n" + "=" * 100)
            return 2
        runtime.report_gpus(log=log)
        orch = Orchestrator(cfg, info, a, RESULTS)
        orch.models_ok = models_ok
        try:
            orch.run()
        except RuntimeError as e:
            log(f"STOPPED: {e}")
            rc = 1
        if orch.failed:
            rc = 1
    finally:
        STOP.set()
        kill_children()
        if orch is not None:
            orch.server.stop(release=True)
            orch._save_budget()
            log("vLLM server stopped, GPU locks released")
    # aggregation needs no GPU (and is cheap): always refresh the tables/figures
    subprocess.call([sys.executable, "-m", "race.aggregate"] + (["--config", a.config] if a.config else []), cwd=ROOT)
    log(f"finished in {(time.time() - t0) / 3600:.1f} h; incomplete: {orch.failed if orch else '-'}; "
        f"see {RESULTS.relative_to(ROOT)}/summary.md (exit {rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
