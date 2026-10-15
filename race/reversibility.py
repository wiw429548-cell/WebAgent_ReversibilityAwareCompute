"""Reversibility predictor: P(action has an irreversible server-side side effect | page, action) BEFORE executing it.

Features: action type, target element role / accessible name / ancestor context, in-form flag, typed text length,
URL path tokens, keyword cues. Labels come for free from executed actions (StateProbe DB diff / network mutation),
collected on WebArena tasks whose intent templates are disjoint from the evaluation tasks.
Variants: 'learned' (logistic regression on hashed features; must have been trained -- no silent fallback),
'heuristic' (keyword rules, the confirm_rule baseline), 'llm' (zero-shot LLM judgement, ablation).
"""
import argparse
import json
import pickle
import re
import urllib.parse

import numpy as np

from .utils import DATA, get_logger, load_config, save_json, DEBUG, RESULTS

MODEL = DATA / ("reversibility_model_debug.pkl" if DEBUG else "reversibility_model.pkl")
ACTION_RE = re.compile(r"^\s*(\w+)\((.*)\)\s*$", re.S)
MUTATING = ["submit", "save", "delete", "remove", "post", "place order", "checkout", "pay", "add to cart", "add to wish",
            "update", "confirm", "send", "create", "merge", "approve", "publish", "follow", "subscribe", "vote", "upvote",
            "downvote", "comment", "reply", "cancel", "close issue", "reopen", "assign", "invite", "apply", "archive",
            "star", "fork", "edit", "change", "reorder", "hold", "unhold", "ship", "invoice", "refund", "block", "join",
            "leave", "upload", "rename", "transfer", "set", "add"]
NAVIGATION = {"go_back", "go_forward", "goto", "new_tab", "tab_close", "tab_focus", "scroll", "noop", "hover", "focus"}


def parse_action(a):
    m = ACTION_RE.match(a or "")
    if not m:
        return "noop", []
    name, args = m.group(1), m.group(2)
    vals = re.findall(r"""'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|(-?\d+(?:\.\d+)?)""", args)
    return name, [x[0] or x[1] or x[2] for x in vals]


def features(action, axmap, url):
    name, args = parse_action(action)
    f = {f"act={name}": 1.0}
    bid = args[0] if args else None
    el = axmap.get(str(bid), {}) if bid is not None else {}
    role, nm, ctx = el.get("role", ""), (el.get("name") or "").lower(), (el.get("context") or "").lower()
    f[f"role={role}"] = 1.0
    if el.get("in_form"):
        f["in_form"] = 1.0
    for w in re.findall(r"[a-z]+", nm)[:12]:
        f[f"name:{w}"] = 1.0
    for w in re.findall(r"[a-z]+", ctx)[:12]:
        f[f"ctx:{w}"] = 0.5
    for kw in MUTATING:
        if kw in nm:
            f[f"kw:{kw}"] = 1.0
    if name in ("fill", "type") and len(args) > 1:
        f["typed_len"] = min(1.0, len(args[1]) / 200)
    if name == "press" and len(args) > 1 and "enter" in args[1].lower():
        f["press_enter"] = 1.0
    path = urllib.parse.urlparse(url or "").path.lower()
    for t in re.findall(r"[a-z]+", path)[:8]:
        f[f"path:{t}"] = 0.5
    f["is_nav"] = float(name in NAVIGATION)
    return f


def heuristic_prob(action, axmap, url):
    name, args = parse_action(action)
    if name in NAVIGATION or name in ("send_msg_to_user", "report_infeasible"):
        return 0.02
    if name in ("fill", "select_option", "clear", "check", "uncheck"):
        return 0.1  # editing a field is reversible until the form is submitted
    el = axmap.get(str(args[0]), {}) if args else {}
    nm = (el.get("name") or "").lower()
    if name == "press" and len(args) > 1 and "enter" in args[1].lower() and el.get("in_form"):
        return 0.6
    if name in ("click", "dblclick") and el.get("role") in ("button", "menuitem") and any(k in nm for k in MUTATING):
        return 0.8
    return 0.1


LLM_RHO = ("A web agent on page {url} is about to execute `{action}` on element [{role}] \"{name}\" (context: {ctx}).\n"
           "Will this action change data on the website's server in a way that cannot be undone by simply navigating "
           "back (e.g. submitting a form, placing an order, posting, deleting, saving settings)? Answer with a "
           "probability between 0 and 1 only.")


class Predictor:
    """source: learned (trained on the collection split; must exist) | heuristic | llm"""

    def __init__(self, source="learned", llm=None):
        self.source, self.llm, self.model = source, llm, None
        if source == "learned":
            if not MODEL.exists():
                raise FileNotFoundError(f"learned reversibility model {MODEL} missing: run the collection split and "
                                        f"`python -m race.reversibility` first (run_all.py does this)")
            with open(MODEL, "rb") as f:
                self.model = pickle.load(f)
        elif source == "llm" and llm is None:
            raise ValueError("rho_source llm needs an LLM client")
        elif source not in ("heuristic", "llm"):
            raise ValueError(f"unknown rho_source {source}")

    def describe(self):
        d = {"source": self.source}
        if self.source == "learned":
            d.update(path=MODEL.name, mtime=MODEL.stat().st_mtime)
        return d

    def prob(self, action, axmap, url, goal=""):
        if self.source == "llm":
            name, args = parse_action(action)
            el = axmap.get(str(args[0]), {}) if args else {}
            q = LLM_RHO.format(url=url, action=action, role=el.get("role", ""), name=el.get("name", ""),
                               ctx=el.get("context", ""))
            out = self.llm.chat([{"role": "user", "content": q}], max_tokens=8, tag="rho")[0]
            m = re.search(r"[01](?:\.\d+)?", out)
            return float(m.group()) if m else 0.5
        if self.source == "learned":
            vec, clf = self.model
            return float(clf.predict_proba(vec.transform([features(action, axmap, url)]))[0, 1])
        return heuristic_prob(action, axmap, url)


# ------------------------------------------------------------------------------------------- training / evaluation
def load_steps(dirs):
    from .env import task_tables
    _, full = task_tables()
    rows = []
    for d in dirs:
        for p in sorted(d.rglob("*.json")):
            ep = json.load(open(p, encoding="utf-8"))
            if "steps" not in ep or ep.get("env_failure"):  # labels of broken/hung episodes are unreliable
                continue
            for st in ep.get("steps", []):
                for ex in st.get("executed", []):
                    if ex.get("feat") is not None:
                        rows.append({"feat": ex["feat"], "y": int(ex["state_changed"]), "site": ep["sites"][0],
                                     "src": ex.get("label_source"), "net": int(ex.get("net_mutation", 0)),
                                     "action": ex["action"], "url": ex.get("url", ""),
                                     "element": ex.get("element") or {}, "task": ep["task_id"],
                                     "template": full.get(ep["task_id"], {}).get("intent_template_id",
                                                                                 f"task{ep['task_id']}")})
    return rows


def fit(rows):
    from sklearn.feature_extraction import DictVectorizer
    from sklearn.linear_model import LogisticRegression
    vec = DictVectorizer()
    X = vec.fit_transform([r["feat"] for r in rows])
    y = np.array([r["y"] for r in rows])
    clf = LogisticRegression(max_iter=3000, C=1.0, class_weight="balanced").fit(X, y)
    return vec, clf


def prf(p, y, thr=0.5):
    """precision / recall / F1 of 'state-changing' at the operating threshold (tau_rho = 0.5)"""
    p, y = np.asarray(p, float), np.asarray(y, int)
    hit = p >= thr
    tp = int((hit & (y == 1)).sum())
    prec = tp / hit.sum() if hit.sum() else None
    rec = tp / (y == 1).sum() if (y == 1).sum() else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
    return {"precision@0.5": None if prec is None else float(prec), "recall@0.5": None if rec is None else float(rec),
            "f1@0.5": None if f1 is None else float(f1),
            "fpr@0.5": float(hit[y == 0].mean()) if (y == 0).any() else None}


def evaluate(rows):
    """leave-one-site-out AUROC / recall at the operating threshold"""
    from sklearn.metrics import average_precision_score, roc_auc_score
    res = {"n": len(rows), "n_tasks": len({r["task"] for r in rows}), "n_templates": len({r["template"] for r in rows}),
           "pos_rate": float(np.mean([r["y"] for r in rows])) if rows else 0.0,
           "label_sources": {s: sum(str(r["src"]) == s for r in rows) for s in sorted({str(r["src"]) for r in rows})},
           "db_vs_network_agreement": float(np.mean([r["y"] == r["net"] for r in rows if r["src"] == "db"]))
           if any(r["src"] == "db" for r in rows) else None, "by_site": {}}
    for site in sorted({r["site"] for r in rows}):
        tr = [r for r in rows if r["site"] != site]
        te = [r for r in rows if r["site"] == site]
        y = np.array([r["y"] for r in te])
        if len(set(y)) < 2 or len({r["y"] for r in tr}) < 2:
            continue
        vec, clf = fit(tr)
        p = clf.predict_proba(vec.transform([r["feat"] for r in te]))[:, 1]
        res["by_site"][site] = {"n": len(te), "pos": int(y.sum()), "auroc_learned": float(roc_auc_score(y, p)),
                                "ap_learned": float(average_precision_score(y, p)),
                                "recall_irrev@0.5": float((p[y == 1] >= 0.5).mean()),
                                "fpr@0.5": float((p[y == 0] >= 0.5).mean()),
                                "precision@0.5": prf(p, y)["precision@0.5"], "f1@0.5": prf(p, y)["f1@0.5"]}
    return res


def _el(r):
    args = parse_action(r["action"])[1]
    return {str(args[0]): r["element"]} if args else {}


def compare_predictors(rows, cfg, n_llm=300, seed=0, use_llm=True):
    """learned (5-fold grouped by intent template: no template in both train and test) vs keyword heuristic vs LLM
    zero-shot on the same actions + learning curve"""
    from sklearn.metrics import average_precision_score, roc_auc_score
    rng = np.random.RandomState(seed)
    groups = sorted({r["template"] for r in rows}, key=str)
    rng.shuffle(groups)
    gfold = {g: i % 5 for i, g in enumerate(groups)}
    fold = {r["task"]: gfold[r["template"]] for r in rows}
    tasks = sorted({r["task"] for r in rows})
    rng.shuffle(tasks)
    y = np.array([r["y"] for r in rows])
    p_learn = np.full(len(rows), np.nan)
    for k in range(5):
        tr = [r for r in rows if fold[r["task"]] != k]
        te = [i for i, r in enumerate(rows) if fold[r["task"]] == k]
        if not te or len({r["y"] for r in tr}) < 2:
            continue
        vec, clf = fit(tr)
        p_learn[te] = clf.predict_proba(vec.transform([rows[i]["feat"] for i in te]))[:, 1]
    ok = ~np.isnan(p_learn)
    p_heur = np.array([heuristic_prob(r["action"], _el(r), r["url"]) for r in rows])

    def met(p, idx):
        if len(set(y[idx])) < 2:
            return {"AUROC": None, "AP": None, "n": int(len(y[idx]))}
        return {"AUROC": float(roc_auc_score(y[idx], p[idx])), "AP": float(average_precision_score(y[idx], p[idx])),
                **prf(p[idx], y[idx]), "n": int(len(y[idx]))}
    idx = np.where(ok)[0]
    res = {"learned (5-fold by template)": met(p_learn, idx), "keyword heuristic": met(p_heur, idx)}
    if use_llm:
        try:  # LLM zero-shot needs the served model
            from .llm import LLMClient
            pred = Predictor("llm", LLMClient(cfg, lane="reversibility"))
            sub = rng.choice(idx, min(n_llm, len(idx)), replace=False)
            p_llm = np.full(len(rows), np.nan)
            p_llm[sub] = [pred.prob(rows[i]["action"], _el(rows[i]), rows[i]["url"]) for i in sub]
            key = f"LLM zero-shot (n={len(sub)})"
            res[key] = met(p_llm, sub)
            res[key]["learned on same subset"] = met(p_learn, sub)["AUROC"]
        except Exception as e:  # noqa
            res["llm_error"] = str(e)[:300]
    # learning curve: AUROC of the learned predictor vs fraction of training tasks
    curve = {}
    for frac in (0.1, 0.25, 0.5, 1.0):
        aucs = []
        for k in range(5):
            tr_tasks = [t for t in tasks if fold[t] != k]
            tr_tasks = set(tr_tasks[:max(2, int(round(frac * len(tr_tasks))))])
            tr = [r for r in rows if r["task"] in tr_tasks]
            te = [i for i, r in enumerate(rows) if fold[r["task"]] == k]
            if not te or len({r["y"] for r in tr}) < 2 or len(set(y[te])) < 2:
                continue
            vec, clf = fit(tr)
            aucs.append(roc_auc_score(y[te], clf.predict_proba(vec.transform([rows[i]["feat"] for i in te]))[:, 1]))
        curve[f"{int(frac * 100)}%"] = float(np.mean(aucs)) if aucs else None
    res["learning_curve_auroc"] = curve
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--no_llm", action="store_true", help="skip the LLM zero-shot comparison")
    a = ap.parse_args()
    cfg = load_config(a.config)
    log = get_logger("reversibility")
    rows = load_steps([RESULTS / "collect"])
    log.info(f"{len(rows)} labelled executed actions from {len({r['task'] for r in rows})} collection tasks")
    classes = {r["y"] for r in rows}
    if len(classes) < 2:
        n_pos = sum(r["y"] for r in rows)
        if not DEBUG:
            raise RuntimeError(f"collected actions contain a single class ({n_pos} state-changing of {len(rows)}); "
                               f"collect more data (collect.n_tasks) or check the StateProbe labels")
        # debug only: the episodes are too few to contain both classes; add one synthetic anchor per class so that
        # the rest of the pipeline can be exercised. Never used for the real results (results/, no RACE_DEBUG).
        log.warning(f"DEBUG: single class ({n_pos}/{len(rows)} positive) -> adding synthetic anchor rows")
        rows = rows + synthetic_anchors()
    ev = evaluate(rows)
    ev["predictor_comparison"] = compare_predictors(rows, cfg, use_llm=not a.no_llm)
    save_json(ev, RESULTS / "reversibility_eval.json")
    export_annotation([r for r in rows if r["src"] != "synthetic"])
    with open(MODEL, "wb") as f:
        pickle.dump(fit(rows), f)
    log.info(f"saved {MODEL}")


def synthetic_anchors():
    """one clearly irreversible and one clearly reversible action (debug runs with a single collected class)"""
    pos_el = {"role": "button", "name": "Submit", "in_form": True, "context": "form"}
    neg_el = {"role": "link", "name": "Next page", "context": "navigation"}
    mk = lambda a, el, url, y: {"feat": features(a, {"1": el}, url), "y": y, "site": "synthetic", "src": "synthetic",
                                "net": y, "action": a, "url": url, "element": el, "task": -1 - y,
                                "template": f"synthetic{y}"}
    return [mk("click('1')", pos_el, "http://localhost/checkout", 1),
            mk("click('1')", neg_el, "http://localhost/catalog", 0)]


def export_annotation(rows, n=200, seed=0):
    """CSV for a human check of the automatic side-effect labels (fill `human_label` with 1 = server state changed)"""
    import csv
    rng = np.random.RandomState(seed)
    pos = [r for r in rows if r["y"] == 1]
    neg = [r for r in rows if r["y"] == 0]
    pick = [pos[i] for i in rng.permutation(len(pos))[:n // 2]] + [neg[i] for i in rng.permutation(len(neg))[:n // 2]]
    out = RESULTS / "annotation"
    out.mkdir(parents=True, exist_ok=True)
    p = out / "side_effect_labels.csv"
    if p.exists():  # never overwrite human labels
        return
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["task", "site", "url", "action", "element_role", "element_name", "auto_label", "label_source",
                    "human_label"])
        for r in pick:
            e = r["element"]
            w.writerow([r["task"], r["site"], r["url"], r["action"], e.get("role"), e.get("name"), r["y"], r["src"], ""])


def score_annotation():
    import csv
    p = RESULTS / "annotation" / "side_effect_labels.csv"
    if not p.exists():
        return None
    rows = [r for r in csv.DictReader(open(p, encoding="utf-8")) if r["human_label"].strip() in ("0", "1")]
    if not rows:
        return None
    a = np.array([int(r["auto_label"]) for r in rows])
    h = np.array([int(r["human_label"]) for r in rows])
    return {"n": len(rows), "agreement": float((a == h).mean()),
            "auto_precision": float(((a == 1) & (h == 1)).sum() / max(1, (a == 1).sum())),
            "auto_recall": float(((a == 1) & (h == 1)).sum() / max(1, (h == 1).sum()))}


if __name__ == "__main__":
    main()
