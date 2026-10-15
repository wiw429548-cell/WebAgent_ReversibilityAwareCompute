"""Offline self-test of the whole RACE pipeline (no GPU, no WebArena, no network; ~30 s):

    python scripts/selftest.py

A fake website and a fake LLM drive the REAL agent / episode code (race.agent, race.runner.run_episode) for every
method and split of configs/default.yaml; then the real predictor training (race.reversibility), the compute-matched
calibration (race.compute_match) and the aggregation (race.aggregate) run on the outputs. Everything happens in a
temporary copy of the project; the real results/ folder is never touched. The numbers it produces are meaningless
(fake site); the test only checks that every stage runs and that every summary table and figure is produced.
"""
import os
import shutil
import subprocess
import tempfile
import hashlib
import json
import random
import re
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if len(sys.argv) < 2:  # parent: make a temporary copy and re-run this script inside it
    tmp = Path(tempfile.mkdtemp(prefix="race_selftest_"))
    for d in ("race", "configs", "scripts"):
        shutil.copytree(SRC / d, tmp / d)
    (tmp / "data").mkdir()
    for f in (SRC / "data").glob("*.json"):
        shutil.copy(f, tmp / "data")
    env = {k: v for k, v in os.environ.items() if k != "RACE_DEBUG"}  # results/ of the copy, not results_debug/
    rc = subprocess.call([sys.executable, str(tmp / "scripts" / "selftest.py"), str(tmp)], env=env)
    print(f"self-test {'PASSED' if rc == 0 else 'FAILED'} (temporary copy: {tmp})")
    sys.exit(rc)
PROJ = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(PROJ))

# ---- stub browsergym.utils.obs.flatten_axtree_to_str (the only browsergym import on the agent path)
def flatten(ax):
    return "\n".join(f"[{n['browsergym_id']}] {n['role']['value']} '{n['name']['value']}'"
                     for n in ax["nodes"] if n.get("browsergym_id") is not None)
for name in ("browsergym", "browsergym.utils", "browsergym.utils.obs"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["browsergym.utils.obs"].flatten_axtree_to_str = flatten

from race import aggregate, compute_match, reversibility, runner  # noqa: E402
from race.agent import Agent  # noqa: E402
from race.llm import Meter  # noqa: E402
from race.utils import RESULTS, load_config, save_json  # noqa: E402

PAGES = {  # url -> [(bid, role, name, effect)]  effect: ("goto", url) | ("change", key) | None
    "/home": [("1", "link", "Products", ("goto", "/list")), ("2", "link", "My Account", ("goto", "/account"))],
    "/list": [("3", "link", "Item A", ("goto", "/item")), ("4", "button", "Add to Cart", ("change", "cart")),
              ("5", "textbox", "Search", None), ("9", "link", "Home", ("goto", "/home"))],
    "/item": [("6", "button", "Submit Review", ("change", "review")), ("7", "link", "Back to list", ("goto", "/list")),
              ("8", "textbox", "Review text", None)],
    "/account": [("10", "button", "Save Address", ("change", "address")), ("11", "textbox", "Street", None),
                 ("9", "link", "Home", ("goto", "/home"))],
}


def obs_of(url, goal, err=""):
    nodes = [{"nodeId": "root", "role": {"value": "RootWebArea"}, "name": {"value": url},
              "childIds": ["form"]},
             {"nodeId": "form", "role": {"value": "form"}, "name": {"value": "main form"},
              "childIds": [f"n{b}" for b, *_ in PAGES[url]]}]
    nodes += [{"nodeId": f"n{b}", "role": {"value": r}, "name": {"value": nm}, "browsergym_id": b, "childIds": []}
              for b, r, nm, _ in PAGES[url]]
    return {"axtree_object": {"nodes": nodes}, "url": "http://site" + url, "goal": goal, "last_action_error": err,
            "open_pages_urls": ["http://site" + url]}


class FakeActionSet:
    def describe(self, **kw):
        return "click(bid), fill(bid, text), goto(url), go_back(), send_msg_to_user(text), report_infeasible(r)"


class FakeEnv:
    def __init__(self, task_id, cfg, probe, override_config=None):
        self.task_id, self.action_set = task_id, FakeActionSet()
        self.state, self.hist = set(), []

    def reset(self, sites):
        self.conf = TASKS[self.task_id]
        self.url = "/home"
        return obs_of(self.url, self.conf["intent"])

    def step(self, action):
        from race.reversibility import parse_action
        name, args = parse_action(action)
        changed, reward, done, err = False, 0.0, False, ""
        if name == "click":
            eff = next((e for b, _, _, e in PAGES[self.url] if b == args[0]), "missing") if args else "missing"
            if eff == "missing":
                err = "element not found"
            elif eff and eff[0] == "goto":
                self.hist.append(self.url)
                self.url = eff[1]
            elif eff and eff[0] == "change":
                changed = True
                self.state.add(eff[1])
                if self.conf["requires_state_change"] and eff[1] == self.conf["target"]:
                    reward, done = 1.0, True
        elif name == "go_back" and self.hist:
            self.url = self.hist.pop()
        elif name == "send_msg_to_user":
            done = True
            reward = float(not self.conf["requires_state_change"] and args and args[0] == "42")
        elif name == "report_infeasible":
            done = True
        side = {"state_changed": changed, "label_source": "db", "net_mutation": changed, "n_requests": 1,
                "server_error": False}
        return obs_of(self.url, self.conf["intent"], err), reward, done, False, {}, side

    def finish(self):
        return {}

    def close(self):
        pass


class FakeLLM:
    def __init__(self, seed=0):
        self.meter, self.truncations, self.model, self.seed = Meter(), 0, "fake", seed

    def chat(self, messages, n=1, temperature=0.0, max_tokens=512, tag=""):
        text = messages[-1]["content"]
        h = int(hashlib.sha1((json.dumps(messages) + tag + str(self.seed)).encode()).hexdigest()[:8], 16)
        rng = random.Random(h)
        base = re.sub(r"\d+$", "", tag)
        outs = []
        for i in range(n):
            if base in ("policy", "m2more"):
                bids = re.findall(r"\[(\d+)\] (\w+) '([^']*)'", text.split("Accessibility tree")[-1])
                opts = [f"click('{b}')" for b, r, _ in bids if r in ("link", "button")]
                opts += [f"fill('{b}', 'hello')" for b, r, _ in bids if r == "textbox"]
                opts += ["send_msg_to_user(\"42\")", "go_back()"]
                k = 0 if temperature == 0 else rng.randrange(len(opts))
                outs.append(f"Thought: step\nAction: {opts[(k + i * (temperature > 0)) % len(opts)]}")
            elif base == "wm":
                outs.append(rng.choice(["The page shows the list.", "An order will be created and saved."]))
            elif base == "value":
                outs.append(f"Score: {rng.randint(0, 10)}")
            elif base == "form":
                outs.append(rng.choice(["OK", "OK", "FIX: fill('8', 'great product')"]))
            elif base == "progress":
                outs.append(rng.choice(["YES", "NO"]))
            elif base == "arbiter":
                outs.append(f"Reason: x\nChoice: {rng.randint(1, 3)}")
            elif base == "rho":
                outs.append(f"{rng.random():.2f}")
            else:
                outs.append("correct")
        self.meter.add(len(json.dumps(messages)) // 4, sum(len(o) for o in outs) // 4, tag=tag)
        return outs


cfg = load_config()
_, full = runner.task_tables()
TASKS = {}
for i, t in full.items():
    rsc = runner.requires_state_change(t)
    TASKS[i] = {"intent": t["intent"], "requires_state_change": rsc, "target": ["cart", "review", "address"][i % 3]}
runner.WebEnv = FakeEnv


def run(method, split, model=None, seed=None, limit=None):
    out = runner.out_dir(split, method, model, seed)
    mc = cfg["methods"][method]
    if mc.get("uniform_m2"):
        cfg["methods"][method] = dict(mc, m2_prob=json.loads((RESULTS / "compute_match.json").read_text())["m2_prob"])
    llm = FakeLLM(seed or 0)
    pred = reversibility.Predictor(mc.get("rho_source", "learned"), llm) if mc.get("race") else None
    agent = Agent(llm, pred, cfg, method)
    ids = runner.task_split(cfg, split)[:limit]
    for i in ids:
        conf, ann = runner.task_config(i, split, full, runner.lite_configs())
        res = runner.run_episode(agent, cfg, i, conf, None, runner.StateProbe())
        assert not res["error"], res["error"]
        res.update(method=method, split=split, model=model or cfg["llm"]["model"], seed=seed, annotation=ann)
        save_json(res, out / f"{i}.json")
    return len(ids)


print("collect", run("tryback_all", "collect"))
reversibility.main.__globals__["sys"] = sys
sys.argv = ["x", "--no_llm"]
reversibility.main()
for m in cfg["main_methods"]:
    if cfg["methods"][m].get("uniform_m2"):
        print("compute match", compute_match.calibrate(cfg))
    print("eval", m, run(m, "eval"))
for m in cfg["ablation_methods"] + cfg["sensitivity_methods"]:
    print("ablation", m, run(m, "ablation"))
for m in cfg["variance_methods"]:
    print("variance", m, run(m, "ablation", seed=cfg["variance_seed"]))
for bb in cfg["extra_backbones"]:
    for m in bb["methods"]:
        print("backbone", m, run(m, bb["split"], model=bb["model"]))
sys.argv = ["x"]
aggregate.main()
expected = ["main", "significance", "cost", "safety", "by_site", "rq1", "ablation", "sensitivity", "backbone",
            "predictor", "predictor_eval", "numbers"]
missing = [t for t in expected if not (RESULTS / "latex" / f"{t}.tex").exists()]
missing += [f for f in ("pareto", "race_modes", "trajectory", "rq1_actions", "predictor_curve")
            if not (RESULTS / "figures" / f"{f}.pdf").exists()]
assert not missing, f"missing outputs: {missing}"
assert (RESULTS / "latex" / "numbers.tex").exists() and (RESULTS / "summary.md").exists()
