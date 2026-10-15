"""Re-judge the string answers whose LLM judgement was deferred (episodes run while the judge model was not served,
i.e. the extra-backbone phase). Run by run_all.py while the judge model (default: the 32B backbone) is served.

For each such episode the string evaluator is re-run offline on the recorded final answer (it only reads the answer, the
intent and the reference answers), the reward becomes  product(other evaluator scores) x new string score, and the
provisional values are kept as reward_provisional / eval_provisional.
"""
import argparse
import json
import os
import tempfile

from .env import lite_configs, task_tables
from .llm import LLMClient
from .runner import task_config
from .utils import RESULTS, get_logger, load_config, save_json

DUMMY = {k: "http://localhost" for k in ("WA_SHOPPING", "WA_SHOPPING_ADMIN", "WA_REDDIT", "WA_GITLAB",
                                          "WA_WIKIPEDIA", "WA_MAP", "WA_HOMEPAGE")}
WEBARENA_ENV = {"SHOPPING": "WA_SHOPPING", "SHOPPING_ADMIN": "WA_SHOPPING_ADMIN", "REDDIT": "WA_REDDIT",
                "GITLAB": "WA_GITLAB", "WIKIPEDIA": "WA_WIKIPEDIA", "MAP": "WA_MAP", "HOMEPAGE": "WA_HOMEPAGE"}


def pending_files(root=None):
    for p in sorted((root or RESULTS).rglob("*.json")):
        if p.parent.name.startswith(".") or p.name.endswith(".tmp"):
            continue
        try:
            ep = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if isinstance(ep, dict) and ep.get("judge_pending") and not ep.get("rejudged"):
            yield p, ep


def string_score(ev_mod, conf, answer):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(conf, f)
        path = f.name
    try:
        return float(ev_mod.StringEvaluator()([{}, {"answer": answer}], path, None, None))
    finally:
        os.unlink(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    cfg = load_config(a.config)
    log = get_logger("rejudge")
    for k, v in DUMMY.items():
        os.environ.setdefault(k, v)
    for k, v in WEBARENA_ENV.items():  # webarena's env_config reads these names
        os.environ.setdefault(k, os.environ[v])
    from . import compat  # noqa: F401
    from . import judge
    import webarena.evaluation_harness.evaluators as ev_mod
    jm = judge.judge_model(cfg)
    if (os.environ.get("RACE_SERVED_MODEL") or jm) != jm:
        raise SystemExit(f"rejudge needs the judge model {jm} to be served")
    judge.install(LLMClient(cfg, model=jm, lane="rejudge"), pending=False)
    _, full = task_tables()
    lite_cfg = lite_configs()
    n = 0
    for p, ep in pending_files():
        conf, _ = task_config(ep["task_id"], ep.get("split", "eval"), full, lite_cfg)
        e = ep.get("eval") or {}
        scores = dict(e.get("scores") or {})
        calls = []
        if "StringEvaluator" in scores:
            judge.reset_episode()
            scores["StringEvaluator"] = string_score(ev_mod, conf, e.get("answer"))
            calls += judge.STATE["calls"]
        html = [c for c in e.get("judge", []) if c.get("src") == "html"]
        if html:  # program_html fuzzy_match targets (all-fuzzy in WebArena-Lite): re-judge the recorded page text
            judge.reset_episode()
            hs = 1.0
            for c in html:
                hs *= judge._judge("fuzzy", c["pred"], c["ref"], c["question"], src="html") if c.get("pending") \
                    else float(c["score"])
            calls += [c for c in html if not c.get("pending")] + judge.STATE["calls"]
            scores["HTMLContentEvaluator"] = hs
        reward = 1.0
        for v in scores.values():
            reward *= float(v)
        ep["reward_provisional"], ep["eval_provisional"] = ep["reward"], e
        ep["reward"] = reward
        ep["success"] = ep["reward"] > 0
        ep["eval"] = {**e, "scores": scores, "judge": calls, "score": ep["reward"]}
        ep["rejudged"] = {"judge_model": jm}
        ep["judge_pending"] = False
        save_json(ep, p)
        n += 1
    log.info(f"re-judged {n} episodes with {jm}")


if __name__ == "__main__":
    main()
