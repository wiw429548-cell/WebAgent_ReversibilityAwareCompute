"""Run one method on a task list (one episode per task, resumable per task: finished tasks are never re-run).

python -m race.runner --method race --split eval [--lane shopping]   # WebArena-Lite (non-map) evaluation tasks
python -m race.runner --method tryback_all --split collect           # data for the reversibility predictor
-> results/<split>/<method>/<task_id>.json

Lanes: tasks are partitioned by site (shopping | shopping_admin | reddit | gitlab | multi = multi-site tasks); run_all
runs the single-site lanes in parallel (each lane resets only its own site). Exit codes: 0 = all tasks of the selection
are done, 1 = some tasks could not be run (environment errors; retried on the next call, saved as env_failure after
3 attempts), 75 = the LLM server is unavailable (nothing saved for the interrupted task), 76 = a WebArena site of the
lane is down / an episode hit the wall-clock timeout (nothing saved; run_all resets or waits, then resumes),
77 = the headless browser (Playwright driver) cannot be started at all (nothing saved: run_all stops instead of
recording every task as env_failure), 2 = configuration problem (e.g. compute_match.json missing for uniform_matched).
Robustness: every task starts only after its sites answer HTTP (waits up to webarena.site_wait_min); an exception in
the middle of an episode, or 5xx pages together with an unreachable site, make the episode an environment error (not
saved, retried); every episode has a wall-clock limit (agent.episode_timeout_min, SIGALRM on POSIX).
Site resets are done by run_all.py (never here).
"""
import argparse
import contextlib
import json
import os
import random
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

from .agent import Agent
from .env import StateProbe, WebEnv, lite_configs, task_tables
from .llm import LLMClient, LLMUnavailable
from .reversibility import Predictor
from .utils import DEBUG, RESULTS, get_logger, load_config, save_json

LANES = ("shopping", "shopping_admin", "reddit", "gitlab", "multi")
MAX_START_ATTEMPTS = 3
EXIT_LLM_DOWN = 75
EXIT_SITE_DOWN = 76
EXIT_BROWSER = 77
EXIT_CONFIG = 2
# the Playwright driver (node) exited before the handshake -> no browser at all (not a task / site problem)
BROWSER_START_SIGNS = ("_get_global_playwright", "'PlaywrightContextManager' object has no attribute")
SITE_ENV = {"shopping": "WA_SHOPPING", "shopping_admin": "WA_SHOPPING_ADMIN", "reddit": "WA_REDDIT",
            "gitlab": "WA_GITLAB", "wikipedia": "WA_WIKIPEDIA", "map": "WA_MAP"}


class EpisodeTimeout(BaseException):
    """BaseException: must not be swallowed by `except Exception` handlers inside BrowserGym / the agent"""


@contextlib.contextmanager
def episode_alarm(seconds, on_hard=None, grace=300):
    """soft timeout: EpisodeTimeout in the main thread. If the process is still inside the episode `grace` seconds
    later (e.g. browser close blocks on a dead Playwright loop), `on_hard()` is called and the process exits with
    EXIT_SITE_DOWN so that run_all restarts the lane in a fresh process (per-task resume)."""
    if not seconds or not hasattr(signal, "SIGALRM"):
        yield
        return

    def on_hard_alarm(sig, frame):
        try:
            if on_hard:
                on_hard()
        finally:
            os._exit(EXIT_SITE_DOWN)

    def on_alarm(sig, frame):
        signal.signal(signal.SIGALRM, on_hard_alarm)
        signal.alarm(int(grace))
        raise EpisodeTimeout(f"episode exceeded {seconds / 60:.0f} min")
    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(int(seconds))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def site_up(site, timeout=20):
    url = os.environ.get(SITE_ENV.get(site, ""), "")
    if not url:
        return True  # unknown URL (e.g. smoke tests): nothing to check
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "race-health"}), timeout=timeout)
        return True
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:  # noqa
        return False


def wait_sites(sites, minutes, log):
    """True once all sites answer (polls every 30 s for up to `minutes`)"""
    t0 = time.time()
    while True:
        down = [s for s in sites if not site_up(s)]
        if not down:
            return True
        if time.time() - t0 > minutes * 60:
            return False
        log.warning(f"sites {down} not reachable -> waiting ({(time.time() - t0) / 60:.1f}/{minutes} min)")
        time.sleep(30)


def browser_start_failure(res):
    err = res.get("error") or ""
    return not res.get("started") and any(s in err for s in BROWSER_START_SIGNS)


def browser_diagnosis():
    """start Playwright + Chromium in a fresh process and return its output (the driver's own stderr)"""
    code = ("from playwright.sync_api import sync_playwright\np = sync_playwright().start()\n"
            "b = p.chromium.launch(headless=True)\nprint('BROWSER_OK', b.version)\nb.close()\np.stop()\n")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
        return f"rc={r.returncode} " + (r.stdout + r.stderr)[-3000:]
    except Exception as e:  # noqa
        return repr(e)


HARNESS_BUG_SIGNS = ("Unknown required_contents",  # program_html fuzzy_match was unsupported (fixed in judge.py)
                     "database disk image is malformed", "file is not a database",  # LLM cache corrupted by two
                     "sqlite3.OperationalError: disk I/O error")  # run_all instances on two hosts (cache errors now ignored)


def harness_bug_failure(res):
    err = res.get("error") or ""
    return any(s in err for s in HARNESS_BUG_SIGNS)


def heal_browser_failures(out):
    """episodes that were saved as env_failure only because the browser could not start, or because of a since-fixed
    harness bug (HARNESS_BUG_SIGNS), are moved to <id>.json.browser_failed / .harness_failed and run again -> number"""
    n = 0
    for p in out.glob("*.json"):
        if not p.stem.isdigit():
            continue
        try:
            e = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa
            continue
        if not e.get("env_failure"):
            continue
        suffix = ".browser_failed" if browser_start_failure(e) else ".harness_failed" if harness_bug_failure(e) else None
        if suffix:
            os.replace(p, p.with_name(p.name + suffix))
            (out / f"{p.stem}.attempts").unlink(missing_ok=True)
            n += 1
    return n


def lane_of(sites):
    return sites[0] if len(sites) == 1 else "multi"


def requires_state_change(t):
    return "program_html" in t["eval"]["eval_types"]


def task_split(cfg, split):
    lite, full = task_tables()
    skip = set(cfg["webarena"]["skip_sites"])
    lite_ids = [t["old_task_id"] for t in lite if not (set(t["sites"]) & skip)]
    if split in ("eval", "ablation"):
        ids = sorted(lite_ids)
        if split == "ablation":  # stratified (by site combination) half of the evaluation tasks
            rng = random.Random(cfg["seed"])
            by = {}
            for i in ids:
                by.setdefault(tuple(full[i]["sites"]), []).append(i)
            keep = {j for g in by.values() for j in rng.sample(g, max(1, round(len(g) * cfg["ablation_subset_frac"])))}
            ids = [i for i in ids if i in keep]
    elif split == "collect":
        # predictor training data: WebArena tasks that are not in WebArena-Lite AND whose intent template does not
        # occur in WebArena-Lite (no template overlap between predictor training and evaluation)
        lite_old = {t["old_task_id"] for t in lite}
        lite_tpl = {t["intent_template_id"] for t in lite} | {full[i]["intent_template_id"] for i in lite_old}
        pool = sorted(i for i, t in full.items() if i not in lite_old and t["intent_template_id"] not in lite_tpl
                      and not (set(t["sites"]) & skip))
        n = cfg["collect"].get("n_tasks")
        if n and n < len(pool):
            rng = random.Random(cfg["seed"] + 1)
            ids = sorted(rng.sample(pool, n))
        else:
            ids = pool
    else:
        raise ValueError(split)
    if DEBUG and split == "collect":
        # debug (--max_tasks N): the first N tasks per lane must produce side-effect labels, i.e. state-changing
        # tasks first -- read-only tasks only yield negatives and the predictor cannot be trained on one class
        ids = sorted(ids, key=lambda i: (not requires_state_change(full[i]), i))
    elif cfg["webarena"].get("order_readonly_first", True):
        # read-only tasks first: they cannot be affected by side effects of earlier state-changing tasks
        ids = sorted(ids, key=lambda i: (requires_state_change(full[i]), i))
    return ids


def out_dir(split, method, model=None, seed=None):
    return RESULTS / split / (method + (f"@{model.split('/')[-1]}" if model else "")
                              + (f"_seed{seed}" if seed is not None else ""))


def task_config(i, split, full, lite_cfg):
    """(config, annotation): WebArena-Lite's corrected annotation for evaluation tasks, the original otherwise"""
    if split in ("eval", "ablation") and i in lite_cfg:
        return lite_cfg[i], "webarena-lite"
    return full[i], "webarena"


def run_episode(agent, cfg, task_id, conf, override, probe):
    from . import judge
    env = WebEnv(task_id, cfg, probe, override_config=override)
    t0 = time.time()
    steps, reward, done = [], 0.0, False
    judge.reset_episode()
    state = {"fixes": 0}
    started = False
    err = None
    delayed = {}
    tags0, hits0 = agent.llm.meter.tags(), agent.llm.meter.cache_hits
    try:
        obs = env.reset(conf["sites"])
        started = True
        goal = obs.get("goal") or conf["intent"]
        history = []
        for k in range(cfg["agent"]["max_steps"]):
            m0 = agent.llm.meter.snapshot()
            obs, reward, done, log = agent.step(env, goal, obs, history, state)
            m1 = agent.llm.meter.snapshot()
            log["llm"] = {key: m1[key] - m0[key] for key in m1}
            steps.append(log)
            if done:
                break
        delayed = env.finish()
    except LLMUnavailable:
        raise
    except Exception:  # noqa
        err = traceback.format_exc()[-3000:]
    finally:
        env.close()
    executed = [ex for s in steps for ex in s["executed"]]
    for k, src in delayed.items():  # changes that appeared only after the action's after-snapshot (sensitivity)
        if 0 <= k < len(executed):
            executed[k]["delayed_change"] = src
    tags1 = agent.llm.meter.tags()
    by_tag = {t: [v[j] - tags0.get(t, [0, 0, 0])[j] for j in range(3)] for t, v in tags1.items()}
    ev = judge.last_eval() or {}
    return {"task_id": task_id, "sites": conf["sites"], "intent": conf["intent"],
            "template": conf.get("intent_template_id"), "eval_types": conf["eval"]["eval_types"],
            "requires_state_change": requires_state_change(conf), "reward": float(reward),
            "success": float(reward) > 0, "terminated": bool(done), "n_steps": len(steps),
            "n_env_actions": len(executed),
            "n_state_changes": sum(ex["state_changed"] for ex in executed),
            "n_state_changes_in_rollbacks": sum(ex["state_changed"] for ex in executed if ex.get("rolled_back")),
            "n_delayed_changes": sum(bool(ex.get("delayed_change")) and not ex["state_changed"] for ex in executed),
            "n_rollbacks": sum(ex["rollback"] for ex in executed),
            "n_server_errors": sum(bool(ex.get("server_error")) for ex in executed),
            "relogins": [ex["relogin"] for ex in executed if ex.get("relogin")],
            "llm_calls": sum(s["llm"]["calls"] for s in steps), "prompt_tokens": sum(s["llm"]["prompt_tokens"] for s in steps),
            "completion_tokens": sum(s["llm"]["completion_tokens"] for s in steps),
            "llm_by_tag": {t: v for t, v in by_tag.items() if v[0]}, "llm_cache_hits": agent.llm.meter.cache_hits - hits0,
            "mode_counts": {md: sum(s["mode"] == md for s in steps) for md in ("M0", "M1", "M2")},
            "n_arbiter": sum("arbiter" in s for s in steps), "n_form_fixes": sum("form_fix" in s for s in steps),
            "eval": ev, "judge_pending": any(c.get("pending") for c in ev.get("judge", [])),
            "label_sources": sorted({ex.get("label_source") for ex in executed}),
            "time_sec": time.time() - t0, "started": started, "error": err, "steps": steps}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", required=True)
    ap.add_argument("--split", default="eval", choices=["eval", "ablation", "collect"])
    ap.add_argument("--lane", default=None, choices=LANES)
    ap.add_argument("--config", default=None)
    ap.add_argument("--max_tasks", type=int, default=None)
    ap.add_argument("--model", default=None, help="backbone override (the served model must match)")
    ap.add_argument("--seed", type=int, default=None, help="sampling seed (run-to-run variance)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    from . import compat  # noqa: F401  (before any webarena import)
    from . import judge
    out = out_dir(a.split, a.method, a.model, a.seed)
    lane = a.lane or "all"
    os.environ["RACE_LANE"] = lane
    log = get_logger(f"{a.split}/{out.name}/{lane}", out / f"run_{lane}.log")
    _, full = task_tables()
    lite_cfg = lite_configs()
    ids = task_split(cfg, a.split)
    if a.lane:
        ids = [i for i in ids if lane_of(full[i]["sites"]) == a.lane]
    ids = ids[:a.max_tasks]
    if out.exists():
        healed = heal_browser_failures(out)
        if healed:
            log.warning(f"{healed} earlier episodes failed only because the browser could not start or because of a "
                        f"fixed harness bug -> re-run")
    todo = [i for i in ids if not (out / f"{i}.json").exists()]
    log.info(f"{len(ids)} tasks in lane {lane}, {len(todo)} to run")
    if not todo:
        return 0
    model = a.model or cfg["llm"]["model"]
    llm = LLMClient(cfg, model=model, seed=a.seed, lane=lane)
    jm = judge.judge_model(cfg)
    served = os.environ.get("RACE_SERVED_MODEL") or model
    judge.install(LLMClient(cfg, model=jm, lane=f"judge_{lane}") if served == jm else None, pending=served != jm)
    mc = cfg["methods"][a.method]
    m2_prob = None
    if mc.get("uniform_m2"):  # compute-matched uniform allocation: p frozen in results/compute_match.json by run_all
        cm_path = RESULTS / "compute_match.json"
        try:
            m2_prob = float(json.loads(cm_path.read_text())["m2_prob"])
        except Exception as e:  # noqa
            log.error(f"{a.method} needs {cm_path} (written by run_all after the race run; or `python -m "
                      f"race.compute_match`): {e}")
            return EXIT_CONFIG
        mc = cfg["methods"][a.method] = dict(mc, m2_prob=m2_prob)
        log.info(f"{a.method}: M2 probability per step p = {m2_prob:.3f} (compute-matched to race)")
    src = mc.get("rho_source", "learned")
    pred = Predictor(src, llm) if mc.get("race") else None
    agent = Agent(llm, pred, cfg, a.method)
    probe = StateProbe.from_env()
    engine = json.loads(os.environ.get("RACE_ENGINE_INFO") or "{}")
    wcfg = cfg["webarena"]
    site_wait = float(wcfg.get("site_wait_min", 10))
    ep_timeout = float(cfg["agent"].get("episode_timeout_min", 60)) * 60

    def bump(i):  # persisted count of environment-error attempts of task i (survives process restarts)
        p = out / f"{i}.attempts"
        n = int(p.read_text()) + 1 if p.exists() else 1
        p.write_text(str(n))
        return n

    def finalize(res):
        res.update(method=a.method, split=a.split, model=model, seed=a.seed, annotation=annotation, lane=lane,
                   judge_model=jm, judge_served=served == jm,
                   predictor=pred.describe() if pred else None, probe_sites=sorted(probe.sites),
                   probe_broken_sites=sorted(probe.broken), probe_info=probe.describe(), engine=engine, m2_prob=m2_prob,
                   llm_truncations=llm.truncations, finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        save_json(res, out / f"{res['task_id']}.json")

    def failure_record(i, conf, error):  # episode without usable trajectory (hung / killed)
        return {"task_id": i, "sites": conf["sites"], "intent": conf["intent"],
                "template": conf.get("intent_template_id"), "eval_types": conf["eval"]["eval_types"],
                "requires_state_change": requires_state_change(conf), "reward": 0.0, "success": False,
                "terminated": False, "n_steps": 0, "n_env_actions": 0, "n_state_changes": 0,
                "n_state_changes_in_rollbacks": 0, "n_delayed_changes": 0, "n_rollbacks": 0, "n_server_errors": 0,
                "relogins": [], "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "llm_by_tag": {},
                "llm_cache_hits": 0, "mode_counts": {"M0": 0, "M1": 0, "M2": 0}, "n_arbiter": 0,
                "n_form_fixes": 0, "eval": {}, "judge_pending": False, "label_sources": [], "time_sec": ep_timeout,
                "started": False, "error": error, "steps": [], "env_failure": True}

    warned_probe = set()
    for i in todo:
        conf, annotation = task_config(i, a.split, full, lite_cfg)
        override = conf if annotation == "webarena-lite" else None
        if not wait_sites(conf["sites"], site_wait, log):
            log.error(f"task {i}: sites {conf['sites']} unreachable for {site_wait:.0f} min -> exit {EXIT_SITE_DOWN}")
            return EXIT_SITE_DOWN

        def hard_kill(i=i, conf=conf):  # the episode did not return after its timeout: count it, maybe give up
            n = bump(i)
            log.error(f"task {i}: still blocked {ep_timeout / 60 + 5:.0f} min after start (attempt {n}) -> "
                      f"process exit {EXIT_SITE_DOWN}")
            if n >= MAX_START_ATTEMPTS:
                finalize(failure_record(i, conf, "EpisodeTimeout: process blocked after the timeout (killed)"))

        timed_out = False
        try:
            with episode_alarm(ep_timeout, on_hard=hard_kill):
                res = run_episode(agent, cfg, i, conf, override, probe)
        except LLMUnavailable as e:
            log.error(f"task {i}: LLM server unavailable ({e}) -> exit {EXIT_LLM_DOWN}, task not saved")
            return EXIT_LLM_DOWN
        except EpisodeTimeout as e:
            timed_out = True
            res = {"error": f"EpisodeTimeout: {e}", "steps": None}
        if browser_start_failure(res):  # nothing to do with the task: never counted / saved
            log.error(f"task {i}: the headless browser could not be started (Playwright driver exited) -> not saved, "
                      f"exit {EXIT_BROWSER}\n{res['error'][-800:]}")
            log.error(f"fresh-process browser check: {browser_diagnosis()}")
            return EXIT_BROWSER
        # environment errors are never scored as agent failures (unless they persist): an exception in the episode,
        # the wall-clock timeout, or server error pages while a site of the task is down
        env_down = bool(res.get("n_server_errors")) and not all(site_up(s) for s in conf["sites"])
        if res["error"] or env_down:
            n = bump(i)
            why = res["error"][-600:] if res["error"] else "5xx pages and the site is not reachable"
            sick = timed_out or env_down or not all(site_up(s) for s in conf["sites"])
            if n < MAX_START_ATTEMPTS:
                log.warning(f"task {i}: environment error (attempt {n}), not saved: {why}")
                if sick:  # fresh process (browser / playwright loop) after a timeout or a site outage
                    log.error(f"task {i}: {'timeout' if timed_out else 'site down'} -> exit {EXIT_SITE_DOWN}")
                    return EXIT_SITE_DOWN
                continue
            if res.get("steps") is None:  # timed out: nothing to report but the failure itself
                res = failure_record(i, conf, res["error"])
            res["env_failure"] = True
            log.warning(f"task {i}: environment error {n} times -> saved as env_failure (counts as unsuccessful)")
        finalize(res)
        for s, qs in probe.dropped.items():
            if s not in warned_probe:
                warned_probe.add(s)
                log.warning(f"DB probe of {s}: {len(qs)} sub-queries fail in this image and are not used: {qs}")
        log.info(f"task {i}: success={res['success']} steps={res['n_steps']} state_changes={res['n_state_changes']} "
                 f"tokens={res['prompt_tokens']}+{res['completion_tokens']} {'ERROR' if res['error'] else ''}")
    missing = [i for i in ids if not (out / f"{i}.json").exists()]
    if missing:
        log.warning(f"{len(missing)} tasks not finished (environment errors) -> they are retried on the next call")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
