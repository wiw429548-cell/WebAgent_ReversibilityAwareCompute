"""Study-level analyses on top of race/aggregate.py.

Reads the raw episode logs in results/{eval,ablation}, recomputes the study-level statistics and
figures, and writes them to stats.json (plus wm_foresight_coding.csv, the per-action coding of the world-model
outcomes). Run from anywhere:  python analysis/analysis.py
"""
import collections
import csv
import json
import os
import pickle
import re
import sys
from pathlib import Path

import numpy as np
from scipy.stats import binomtest, norm

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # repository root
RES = ROOT / "results"
sys.path.insert(0, str(ROOT))
from race.reversibility import heuristic_prob, parse_action  # noqa: E402

MAIN = ["react", "sc_vote3", "catts", "wm_all", "tryback_all", "confirm_rule", "race", "uniform_matched"]
NAV = {"go_back", "go_forward", "goto", "new_tab", "tab_close", "tab_focus", "scroll", "noop", "hover", "focus",
       "send_msg_to_user", "report_infeasible"}
EDIT = {"fill", "select_option", "clear", "check", "uncheck", "type"}
MUT_WORDS = re.compile(r"(submit|save|delete|remove|post|place order|checkout|add to|update|confirm|send|create|merge|"
                       r"approve|publish|follow|subscribe|vote|comment|reply|cancel|close|reopen|assign|invite|"
                       r"archive|star|fork|reorder|commit|set status|apply|join|leave|upload|rename)", re.I)


def load(split, run):
    out = []
    for p in sorted((RES / split / run).glob("*.json")):
        if p.stem.isdigit():
            out.append(json.load(open(p, encoding="utf-8")))
    return out


def ier(e):
    return bool(e["n_state_changes"] > 0 and (not e["requires_state_change"] or not e["success"]))


def tokens(e):
    return e["prompt_tokens"] + e["completion_tokens"]


def boot(x, n=2000, seed=0):
    x = np.asarray(x, float)
    rng = np.random.RandomState(seed)
    b = x[rng.randint(0, len(x), (n, len(x)))].mean(1)
    return float(np.quantile(b, .025)), float(np.quantile(b, .975))


def executed(eps):
    for e in eps:
        for si, st in enumerate(e["steps"]):
            for ex in st["executed"]:
                yield e, si, st, ex


def cue_class(ex):
    name, _ = parse_action(ex["action"])
    el = ex.get("element") or {}
    nm = el.get("name") or ""
    if name in NAV:
        return "navigation / answer"
    if name in EDIT:
        return "fill / select"
    if name == "press":
        return "key press"
    if name in ("click", "dblclick"):
        if MUT_WORDS.search(nm):
            return "click: mutating label (link)" if el.get("role") == "link" else "click: mutating label"
        return "click: other label"
    return "other"


def main():
    S = {}
    runs = {m: load("eval", m) for m in MAIN}
    ids = sorted(runs["react"][i]["task_id"] for i in range(len(runs["react"])))
    S["n_tasks"] = len(ids)
    S["n_readonly"] = sum(not e["requires_state_change"] for e in runs["react"])
    S["n_changing"] = S["n_tasks"] - S["n_readonly"]

    # ---------------------------------------------------------------- per-method headline metrics with CIs
    meth = {}
    for m, eps in runs.items():
        sr = [float(e["success"]) for e in eps]
        ie = [float(ier(e)) for e in eps]
        meth[m] = {"SR": np.mean(sr), "SR_ci": boot(sr), "IER": np.mean(ie), "IER_ci": boot(ie),
                   "tokens": float(np.mean([tokens(e) for e in eps])),
                   "tok_ci": boot([tokens(e) for e in eps])}
    S["methods"] = meth
    # audit: task 641's post is correct but the open-model fuzzy judge scores it "partially correct"; task 791
    # creates a folder but its evaluator type marks it read-only. IER without these two tasks:
    AUDIT = {641, 791}
    S["ier_audited"] = {m: float(np.mean([ier(e) for e in eps if e["task_id"] not in AUDIT])) for m, eps in runs.items()}

    # ---------------------------------------------------------------- RQ1: where side effects happen (all main runs)
    acts = [(m, e, si, st, ex) for m in MAIN for (e, si, st, ex) in executed(runs[m])]
    S["n_actions"] = len(acts)
    chg = [a for a in acts if a[4]["state_changed"]]
    S["n_changes"] = len(chg)
    cues = collections.defaultdict(lambda: [0, 0])
    for a in acts:
        c = cues[cue_class(a[4])]
        c[0] += 1
        c[1] += bool(a[4]["state_changed"])
    S["by_cue"] = {k: {"n": v[0], "share": v[0] / len(acts), "p_change": v[1] / v[0], "changes": v[1]}
                   for k, v in cues.items()}
    names = collections.Counter()
    roles = collections.Counter()
    for a in chg:
        el = a[4].get("element") or {}
        nm = re.sub(r"[-]", "", el.get("name") or "").strip() or "(unnamed)"
        names[(nm, el.get("role") or "?")] += 1
        roles[el.get("role") or "?"] += 1
    S["culprit_names"] = [[n, r, c] for (n, r), c in names.most_common(14)]
    S["culprit_roles"] = dict(roles)
    # HTTP semantics: is every DB change accompanied by a non-GET request?
    S["http"] = {"db_change_with_mutating_request": sum(bool(a[4].get("net_mutation")) for a in chg),
                 "db_change": len(chg),
                 "mutating_request_actions": sum(bool(a[4].get("net_mutation")) for a in acts),
                 "mutating_request_no_change": sum(bool(a[4].get("net_mutation")) and not a[4]["state_changed"]
                                                   for a in acts)}
    # repeated commits: a state change on an element label that already changed state earlier in the episode
    rep = 0
    for m in MAIN:
        for e in runs[m]:
            seen = set()
            for _, _, ex in ((None, None, ex) for st in e["steps"] for ex in st["executed"]):
                if ex["state_changed"]:
                    nm = re.sub(r"[-]", "", (ex.get("element") or {}).get("name") or "").strip().lower()
                    rep += nm in seen
                    seen.add(nm)
    S["repeat_changes"] = rep

    # ---------------------------------------------------------------- IER anatomy
    anat = {}
    task_hits = collections.Counter()
    for m in MAIN:
        c = collections.Counter()
        for e in runs[m]:
            if not ier(e):
                continue
            task_hits[e["task_id"]] += 1
            k = sum(ex["state_changed"] for st in e["steps"] for ex in st["executed"])
            if not e["requires_state_change"]:
                c["collateral"] += 1
            elif k > 1:
                c["repeated"] += 1
            else:
                c["wrong_commit"] += 1
        anat[m] = dict(c)
    S["ier_anatomy"] = anat
    S["ier_episodes"] = sum(task_hits.values())
    S["ier_tasks"] = len(task_hits)
    S["ier_task_hits"] = sorted(task_hits.items(), key=lambda x: -x[1])
    S["ier_in_tasks_hit_by_ge6"] = sum(v for v in task_hits.values() if v >= 6)
    S["tasks_hit_by_all8"] = [t for t, v in task_hits.items() if v == 8]

    # RACE's IER culprit actions by mode / predicted rho
    rc = collections.Counter()
    for e in runs["race"]:
        if ier(e):
            for st in e["steps"]:
                for ex in st["executed"]:
                    if ex["state_changed"]:
                        rc[st["mode"]] += 1
    S["race_culprit_modes"] = dict(rc)

    # ---------------------------------------------------------------- RQ2: predictors on all executed eval actions
    with open(ROOT / "data" / "reversibility_model.pkl", "rb") as f:
        vec, clf = pickle.load(f)
    rows = [a for a in acts if a[4].get("feat") is not None]
    y = np.array([int(a[4]["state_changed"]) for a in rows])
    p_l = clf.predict_proba(vec.transform([a[4]["feat"] for a in rows]))[:, 1]
    p_h = np.array([heuristic_prob(a[4]["action"], {str(x): a[4].get("element") or {} for x in
                                                    parse_action(a[4]["action"])[1][:1]}, a[4].get("url", ""))
                    for a in rows])
    g = np.array([bool(a[4].get("net_mutation")) for a in rows])

    def curve(p):
        out = []
        for t in sorted(set(np.round(p, 4))) + [1.01]:
            f = p >= t
            out.append([float(f.mean()), float((f & (y == 1)).sum() / max(1, y.sum())), float(t)])
        return out
    S["pred_curves"] = {"learned": curve(p_l), "heuristic": curve(p_h),
                        "http_gate": [[float(g.mean()), float((g & (y == 1)).sum() / y.sum()), 0.5]]}
    for nm, p in (("learned", p_l >= .5), ("heuristic", p_h >= .5), ("http_gate", g)):
        tp = int((p & (y == 1)).sum())
        S.setdefault("pred_points", {})[nm] = {"flag_share": float(p.mean()), "recall": tp / y.sum(),
                                               "precision": tp / max(1, p.sum())}
    S["pred_n"], S["pred_pos"] = int(len(y)), int(y.sum())
    # recall of the heuristic on link-borne changes
    link = np.array([(a[4].get("element") or {}).get("role") == "link" for a in rows])
    S["heuristic_recall_links"] = float(((p_h >= .5) & (y == 1) & link).sum() / max(1, ((y == 1) & link).sum()))
    S["learned_recall_links"] = float(((p_l >= .5) & (y == 1) & link).sum() / max(1, ((y == 1) & link).sum()))

    # ---------------------------------------------------------------- world-model foresight (executed M2 choices)
    MISSING = re.compile(r"no element|does not (exist|correspond)|not (listed|present|found)|there might be a mistake",
                         re.I)
    NOCHG = re.compile(r"\bno (new )?data\b[^.]{0,40}\b(will|would|is|are)\b|(will|would|does|do) not (create|modify|"
                       r"delete|change|alter|trigger)|without (creat|modif|chang|alter)|no (changes|modifications)\b"
                       r"[^.]{0,30}\b(will|would)|nothing (will|would) be (created|modified|deleted)", re.I)
    ANN = re.compile(r"add(ed|s|ing)? (the|this|it|to)|creat|submit|post(ed|ing)?\b|sav(e|ed|ing)|updat|"
                     r"decrease|increase|reorder|process|delet|remov|subscrib|vote", re.I)
    coding = []
    for m in MAIN:
        for e in runs[m]:
            for si, st in enumerate(e["steps"]):
                if st.get("mode") != "M2" or "m2_cands" not in st or not st["executed"]:
                    continue
                ch = st.get("m2_choice")
                if ch not in st["m2_cands"] or "outcomes" not in st or st.get("form_fix") \
                        or st["executed"][0].get("action") != ch:
                    continue
                out = str(st["outcomes"][st["m2_cands"].index(ch)])
                if MISSING.search(out):
                    code = "missing_element"
                elif NOCHG.search(out):
                    code = "no_change"
                elif ANN.search(out):
                    code = "announced"
                else:
                    code = "no_change"
                coding.append({"method": m, "task": e["task_id"], "step": si, "action": ch,
                               "state_changed": bool(st["executed"][0]["state_changed"]), "code": code,
                               "outcome": out[:300].replace("\n", " ")})
    with open(HERE / "wm_foresight_coding.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(coding[0].keys()))
        w.writeheader()
        w.writerows(coding)
    pos = [c for c in coding if c["state_changed"]]
    neg = [c for c in coding if not c["state_changed"]]
    S["wm"] = {"n": len(coding), "pos": len(pos),
               "pos_codes": dict(collections.Counter(c["code"] for c in pos)),
               "neg_announced": sum(c["code"] == "announced" for c in neg), "neg": len(neg)}
    # how often lookahead overrode the top-voted action; value-score separation
    ov = {}
    for m in ("wm_all", "race", "uniform_matched", "confirm_rule"):
        n = o = 0
        for e in runs[m]:
            for st in e["steps"]:
                if st.get("mode") == "M2" and st.get("candidates") and "m2_choice" in st:
                    n += 1
                    o += st["m2_choice"] != st["candidates"][0][0]
        ov[m] = {"m2_steps": n, "override": o, "form_fixes": sum(e.get("n_form_fixes", 0) for e in runs[m])}
    S["m2_override"] = ov
    gaps = [max(s) - sorted(s)[-2] for e in runs["wm_all"] for st in e["steps"]
            if (s := st.get("wm_scores")) and len(s) > 1]
    S["value_gap"] = {"n": len(gaps), "median": float(np.median(gaps)), "share_lt_0.5": float(np.mean(np.array(gaps) < .5))}

    # ---------------------------------------------------------------- variance and power
    race = {e["task_id"]: e for e in runs["race"]}
    s2 = {e["task_id"]: e for e in load("ablation", "race_seed2027")}
    x = np.array([race[i]["success"] for i in sorted(s2)])
    z = np.array([s2[i]["success"] for i in sorted(s2)])
    b, c = int((x & ~z).sum()), int((~x & z).sum())
    S["seed"] = {"n": len(x), "SR_a": float(x.mean()), "SR_b": float(z.mean()), "b": b, "c": c,
                 "p": float(binomtest(b, b + c).pvalue)}
    disc = []
    for m in MAIN:
        if m == "race":
            continue
        o = {e["task_id"]: e for e in runs[m]}
        xs = np.array([race[i]["success"] for i in ids])
        zs = np.array([o[i]["success"] for i in ids])
        disc.append(float((xs != zs).mean()))
    S["discordance_SR"] = {"mean": float(np.mean(disc)), "min": float(min(disc)), "max": float(max(disc))}

    def n_req(delta, d, alpha=.05, power=.8):
        za, zb = norm.ppf(1 - alpha / 2), norm.ppf(power)
        return float(((za * np.sqrt(d) + zb * np.sqrt(d - delta ** 2)) / delta) ** 2)
    S["power"] = {f"{d:.2f}": {f"{dl:.2f}": n_req(dl, d) for dl in (0.02, 0.03, 0.05, 0.08, 0.10)}
                  for d in (0.05, 0.10, 0.15)}
    S["power_curve"] = {f"{d:.2f}": [[dl, n_req(dl, d)] for dl in np.linspace(0.015, 0.15, 60)]
                        for d in (0.05, 0.10, 0.15)}

    with open(HERE / "stats.json", "w", encoding="utf-8") as f:
        json.dump(S, f, indent=1, default=float)
    print(json.dumps({k: v for k, v in S.items() if k not in ("pred_curves", "power_curve")}, indent=1, default=float))


if __name__ == "__main__":
    main()
