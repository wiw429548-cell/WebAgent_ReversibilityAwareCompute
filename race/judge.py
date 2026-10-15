"""LLM judge used by WebArena's string evaluator (`fuzzy_match` / `ua_match`), served by our own vLLM model.

WebArena calls GPT-4-turbo (`gpt-4-1106-preview`) here and asserts on the reply. We keep the original prompts and the
original decision rule but
  * route the call through our cached client (judge model = cfg llm.judge_model, default the 32B backbone),
  * never raise: an unparsable reply scores 0 and is flagged,
  * skip the call for BrowserGym's "whatever" sentinel (validate() is run after EVERY step; without a stop action the
    answer is "whatever" and the score is 0 anyway),
  * if the currently served model is not the judge model (14B backbone phase) the call is recorded as *pending* with a
    provisional score 0 and re-judged at the end by `race.rejudge` with the judge model. The trajectory is unaffected:
    the string evaluator only runs on the stop answer, which terminates the episode regardless of the score.
`EvaluatorComb.__call__` is wrapped to record the per-evaluator scores of the last validation (= final step).
"""
import os

STATE = {"llm": None, "pending": False, "calls": [], "last_eval": None}

FUZZY_SYS = "You are a helpful assistant"


def fuzzy_messages(pred, reference, question):
    message = ("Help a teacher to grade the answer of a student given a question. Keep in mind that the student may "
               "use different phrasing or wording to answer the question. The goal is to evaluate whether the answer "
               "is semantically equivalent to the reference answer.\n")
    message += f"question: {question}\n"
    message += f"reference answer: {reference}\n"
    message += "all the string 'N/A' that you see is a special sequence that means 'not achievable'\n"
    message += f"student answer: {pred}\n"
    message += "Conclude the judgement by correct/incorrect/partially correct."
    return [{"role": "system", "content": FUZZY_SYS}, {"role": "user", "content": message}]


def ua_messages(pred, reference, question):
    message = f"task: {question}\nactual unachievable reason: {reference}\nreported unachievable reason: {pred}\n"
    message += (
        "The task described above is inherently unachievable due to the reason specified under 'actual unachievable "
        "reason'. An individual previously attempted this task and was unable to complete it. They provided a reason "
        "for their failure, which is listed under 'reported unachievable reason'. Your role is to review both the "
        "actual and reported reasons. Determine if the reported reason aligns with the actual reason, even if "
        "implicitly. If the stated reason is in line with the actual reason, respond with 'same'. Otherwise, respond "
        "with 'different'.")
    return [{"role": "system", "content": FUZZY_SYS}, {"role": "user", "content": message}]


def parse(kind, response):
    """-> (score, parsable) with WebArena's decision rule"""
    r = (response or "").lower()
    if kind == "fuzzy":
        if "partially correct" in r or "incorrect" in r:
            return 0.0, True
        return (1.0, True) if "correct" in r else (0.0, False)
    if "different" in r:
        return 0.0, True
    return (1.0, True) if "same" in r else (0.0, False)


def judge_call(llm, kind, pred, reference, question):
    msgs = (fuzzy_messages if kind == "fuzzy" else ua_messages)(pred, reference, question)
    resp = llm.chat(msgs, temperature=0.0, max_tokens=768, tag="judge")[0]
    score, ok = parse(kind, resp)
    return score, ok, resp


def _judge(kind, pred, reference, question, src="string"):
    rec = {"kind": kind, "pred": pred, "ref": reference, "question": question, "src": src}
    if str(pred).strip().lower() == "whatever":
        return 0.0
    if STATE["pending"] or STATE["llm"] is None:
        STATE["calls"].append({**rec, "pending": True, "score": 0.0})
        return 0.0
    score, ok, resp = judge_call(STATE["llm"], kind, pred, reference, question)
    STATE["calls"].append({**rec, "pending": False, "score": score, "parsable": ok, "response": resp[:300],
                           "judge_model": STATE["llm"].model})
    return score


def llm_fuzzy_match(pred, reference, question):
    return _judge("fuzzy", pred, reference, question)


def llm_ua_match(pred, reference, question):
    return _judge("ua", pred, reference, question)


def _html_fuzzy(required, selected, configs):
    ref, question = ", ".join(required), configs.get("intent", "")
    if not str(selected).strip():  # locator found nothing (wrong page) -> no judge call
        STATE["calls"].append({"kind": "fuzzy", "pred": "", "ref": ref, "question": question, "src": "html",
                               "pending": False, "score": 0.0})
        return 0.0
    return _judge("fuzzy", selected, ref, question, src="html")


def _patch_html_fuzzy(ev):
    """WebArena-Lite configs (tasks 528, 641, 645, 657, ...) use program_html `required_contents: {"fuzzy_match": [...]}`,
    which webarena 0.x's HTMLContentEvaluator rejects with ValueError (-> every episode an env_failure). Add the
    WebArena-Lite branch: the selected element is LLM-judged against the comma-joined reference list (same judge as the
    string evaluator)."""
    import inspect
    import re
    import textwrap
    cls = ev.HTMLContentEvaluator
    if getattr(cls, "_race_html_fuzzy", False):
        return
    src = textwrap.dedent(inspect.getsource(cls.__call__))
    m = re.search(r"\n( *)else:\n *raise ValueError\(\n *f\"Unknown required_contents", src)
    if not m:
        raise RuntimeError("webarena HTMLContentEvaluator changed: cannot add the fuzzy_match branch")
    ind = m.group(1)
    branch = (f"\n{ind}elif \"fuzzy_match\" in target[\"required_contents\"]:\n"
              f"{ind}    score *= float(_race_html_fuzzy(target[\"required_contents\"][\"fuzzy_match\"], "
              f"selected_element, configs))")
    src = src[:m.start()] + branch + src[m.start():]
    ns = dict(vars(ev), _race_html_fuzzy=_html_fuzzy)
    exec(compile(src, inspect.getsourcefile(cls), "exec"), ns)
    cls.__call__ = ns["__call__"]
    cls._race_html_fuzzy = True


def install(llm=None, pending=False):
    """patch webarena's evaluators (call after the WA_* variables are set)."""
    from . import compat  # noqa: F401
    for key in ("SHOPPING", "SHOPPING_ADMIN", "REDDIT", "GITLAB", "WIKIPEDIA", "MAP", "HOMEPAGE"):
        if f"WA_{key}" in os.environ:  # webarena's env_config reads the un-prefixed names on import
            os.environ[key] = os.environ[f"WA_{key}"]
    import webarena.evaluation_harness.evaluators as ev
    import webarena.evaluation_harness.helper_functions as hf
    STATE.update(llm=llm, pending=pending)
    ev.llm_fuzzy_match = hf.llm_fuzzy_match = llm_fuzzy_match
    ev.llm_ua_match = hf.llm_ua_match = llm_ua_match
    _patch_html_fuzzy(ev)

    def comb_call(self, trajectory, config_file, page=None, client=None):
        STATE["calls"] = []
        score, parts = 1.0, {}
        for e in self.evaluators:
            s = float(e(trajectory, config_file, page, client))
            parts[type(e).__name__] = s
            score *= s
        STATE["last_eval"] = {"answer": (trajectory[-1] or {}).get("answer"), "scores": parts,
                              "judge": list(STATE["calls"]), "score": score}
        return score

    ev.EvaluatorComb.__call__ = comb_call


def reset_episode():
    STATE["calls"], STATE["last_eval"] = [], None


def last_eval():
    return STATE["last_eval"]


def judge_model(cfg):
    return cfg["llm"].get("judge_model") or cfg["llm"]["model"]


def served_model():
    return os.environ.get("RACE_SERVED_MODEL")
