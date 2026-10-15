"""Calibration of the compute-matched uniform baseline (uniform_matched).

uniform_matched samples and votes like RACE (n=3) and spends RACE's extra compute (M2 imagine-and-verify + form check)
on a random share p of the steps, without any reversibility or uncertainty signal. p is chosen so that the expected
LLM tokens per step equal RACE's measured tokens per step on the evaluation split:

    p = clip((T_race - T_M0) / (T_M2 - T_M0), 0, 1)

with T_race = RACE's mean tokens/step and T_M0 / T_M2 = RACE's mean tokens of its M0 / M2 steps. The value is frozen in
results/compute_match.json the first time it is computed (resumed runs use the same p); the achieved token ratio
(uniform_matched / race tokens per task) is reported by race.aggregate.

python -m race.compute_match [--force]
"""
import argparse
import json
import time

from .utils import DEBUG, RESULTS, load_config, save_json

PATH = RESULTS / "compute_match.json"
MIN_EPISODES = 20


def _tok(st):
    return st["llm"]["prompt_tokens"] + st["llm"]["completion_tokens"]


def calibrate(cfg, ref="race", split="eval", force=False):
    """-> dict (also written to results/compute_match.json) or None if RACE has too few episodes"""
    if PATH.exists() and not force:
        return json.loads(PATH.read_text())
    from .aggregate import load_run
    eps = [e for e in load_run(split, ref) if not e.get("env_failure")]
    steps = [st for e in eps for st in e.get("steps", [])]
    if len(eps) < (2 if DEBUG else MIN_EPISODES) or not steps:
        return None
    by = {md: [_tok(st) for st in steps if st["mode"] == md] for md in ("M0", "M1", "M2")}
    t_ref = sum(_tok(st) for st in steps) / len(steps)
    t_m0 = sum(by["M0"]) / len(by["M0"]) if by["M0"] else None
    t_m2 = sum(by["M2"]) / len(by["M2"]) if by["M2"] else None
    if t_m0 is None or t_m2 is None or t_m2 <= t_m0:
        p, note = 0.0, "RACE has no M0 or M2 steps (or M2 is not more expensive): uniform_matched = plain voting"
    else:
        p, note = min(1.0, max(0.0, (t_ref - t_m0) / (t_m2 - t_m0))), ""
    out = {"m2_prob": round(p, 4), "reference": f"{split}/{ref}", "n_episodes": len(eps), "n_steps": len(steps),
           "tokens_per_step_ref": t_ref, "tokens_per_step_M0": t_m0, "tokens_per_step_M2": t_m2,
           "tokens_per_task_ref": sum(e["prompt_tokens"] + e["completion_tokens"] for e in eps) / len(eps),
           "mode_share_ref": {md: len(v) / len(steps) for md, v in by.items()}, "note": note,
           "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_json(out, PATH)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true", help="recompute even if compute_match.json exists")
    a = ap.parse_args()
    r = calibrate(load_config(a.config), force=a.force)
    print(json.dumps(r, indent=1) if r else "not enough RACE episodes yet (results/eval/race)")
    return 0 if r else 1


if __name__ == "__main__":
    raise SystemExit(main())
