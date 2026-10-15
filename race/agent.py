"""Policies: ReAct, self-consistency voting, CATTS, WebDreamer-style lookahead (wm_all), try-and-backtrack everywhere,
RACE (+ ablations) and the compute-matched uniform allocation (uniform_matched: RACE's M2 compute on random steps).
One `step` call = one agent step (may execute several env actions in mode M1).

Modes
  M0 : execute the (voted) top candidate
  M1 : try-and-backtrack in the real environment (only allowed when the action is predicted reversible)
  M2 : imagine-and-verify: LLM world model predicts the outcome of each candidate, a value prompt scores it,
       and (RACE) a form-level check verifies field values before an irreversible submission
"""
import hashlib
import math
import re
from collections import Counter

from .env import ax_index, obs_text
from .llm import max_model_len
from .reversibility import NAVIGATION, features, parse_action

SYSTEM = (
    "You are an autonomous web agent operating a real website through its accessibility tree. Each element has a "
    "numeric id in brackets, e.g. [42]. Complete the user's goal. When the goal asks for information, answer with "
    "send_msg_to_user(\"<answer>\"); when the goal is completed, call send_msg_to_user(\"done\"); if the task is "
    "impossible, call report_infeasible(\"<reason>\").\nThe website is a self-hosted replica (e.g. Postmill is the "
    "Reddit-like forum, the store is a Magento shop, GitLab is self-hosted) that runs at the URL shown below; the "
    "public internet (reddit.com, google.com, gitlab.com, ...) is NOT reachable, so never goto() an external domain "
    "-- navigate with the site's own links, menus and search.\n\nAvailable actions:\n{actions}\n\n"
    "Respond exactly in this format:\nThought: <short reasoning>\nAction: <exactly one action call>")

USER = ("Goal: {goal}\n\nCurrent URL: {url}\nOpen tabs: {tabs}\n\nPrevious actions:\n{history}\n{error}\n"
        "Accessibility tree of the current page:\n{ax}\n\nWhat is the next action?")

WM = ("You are a world model of a website. Current URL: {url}\nCurrent page (accessibility tree, truncated):\n{ax}\n\n"
      "The agent will execute: {action}\n\nDescribe in 2-4 sentences what will happen: which page will be shown and "
      "whether any data on the website will be created, modified or deleted (and what exactly).")

VALUE = ("Goal: {goal}\nCurrent URL: {url}\nCandidate action: {action}\nPredicted outcome: {outcome}\n\n"
         "Rate how good this action is for achieving the goal from 0 (useless or harmful, e.g. an unintended or "
         "wrong irreversible change) to 10 (clearly the right next step). Output only: Score: <0-10>")

FORM_CHECK = ("Goal: {goal}\nThe agent is about to execute a possibly irreversible action: {action}\n"
              "Current page (accessibility tree):\n{ax}\n\nCheck every form field / selected option on the page that "
              "this action would submit against the goal (exact quantities, names, addresses, titles, recipients, "
              "prices, dates). If everything matches the goal answer exactly 'OK'. Otherwise answer 'FIX: <one action "
              "that corrects one wrong field>' using the action syntax, e.g. FIX: fill('123', 'correct value').")

PROGRESS = ("Goal: {goal}\nThe agent executed: {action}\n\nPage BEFORE (url {u0}):\n{a0}\n\nPage AFTER (url {u1}):\n{a1}"
            "\n\nDid this action make progress towards the goal (reveal needed information or move closer to "
            "completing it)? Answer YES or NO.")

ARBITER = ("Goal: {goal}\n\nCurrent URL: {url}\n\nPrevious actions:\n{history}\n\nAccessibility tree of the current "
           "page:\n{ax}\n\nSeveral sampled proposals for the next action disagree:\n{cands}\n\nThink about which "
           "proposal best advances the goal on this page, then answer in the format\nReason: <one sentence>\n"
           "Choice: <number>")


def extract_action(text):
    m = re.findall(r"Action:\s*(.+)", text)
    a = m[-1].strip() if m else ""
    if not a:
        c = re.findall(r"```(?:python)?\s*(.+?)```", text, re.S)
        a = c[-1].strip() if c else ""
    a = a.strip().strip("`").split("\n")[0].strip()
    return a or "noop()"


def norm(a):
    return re.sub(r"\s+", "", a.replace('"', "'"))


class Agent:
    def __init__(self, llm, predictor, cfg, method):
        self.llm, self.pred, self.cfg = llm, predictor, cfg
        self.m = cfg["methods"][method]
        self.name = method
        self.rc = {**cfg["race"], **self.m.get("race_overrides", {})}
        self.ac = cfg["agent"]
        # character budget of the main prompt: clamped so that prompt + 384 output tokens fit the served context
        # (~2.5 characters per token for accessibility trees, 4.5k tokens for instructions/history/actions)
        mml = max_model_len(cfg)
        self.max_chars = max(2000, min(int(self.ac["axtree_chars"]), int((mml - 4500) * 2.5)))

    def chars(self, n):
        return min(n, self.max_chars)

    # ------------------------------------------------------------------ prompting
    def messages(self, goal, obs, history, action_desc):
        ax, tabs = obs_text(obs, self.max_chars)
        hist = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(history[-self.ac["history_steps"]:])) or "(none)"
        err = f"Last action error: {obs['last_action_error']}\n" if obs.get("last_action_error") else ""
        return [{"role": "system", "content": SYSTEM.format(actions=action_desc)},
                {"role": "user", "content": USER.format(goal=goal, url=obs.get("url", ""), tabs=tabs, history=hist,
                                                        error=err, ax=ax)}]

    def sample(self, msgs, n, temp, tag):
        outs = self.llm.chat(msgs, n=n, temperature=temp if n > 1 else 0.0, max_tokens=384, tag=tag)
        return [extract_action(o) for o in outs]

    @staticmethod
    def vote(actions):
        c = Counter(norm(a) for a in actions)
        first = {}
        for a in actions:
            first.setdefault(norm(a), a)
        ranked = [(first[k], v) for k, v in c.most_common()]
        n = len(actions)
        u = 0.0
        if n > 1:
            u = max(0.0, -sum((v / n) * math.log(v / n) for _, v in ranked) / math.log(n))
        return ranked, u

    # ------------------------------------------------------------------ M2
    def arbiter(self, goal, obs, history, ranked):
        """CATTS: an LLM arbiter picks among the disagreeing votes (only invoked when uncertain)"""
        ax, _ = obs_text(obs, self.chars(self.ac.get("axtree_chars", 12000)))
        hist = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(history[-self.ac["history_steps"]:])) or "(none)"
        cands = "\n".join(f"{i + 1}. {a}   ({v} vote{'s' if v > 1 else ''})" for i, (a, v) in enumerate(ranked))
        t = self.llm.chat([{"role": "user", "content": ARBITER.format(goal=goal, url=obs.get("url", ""), history=hist,
                                                                       ax=ax, cands=cands)}],
                          max_tokens=160, tag="arbiter")[0]
        m = re.findall(r"Choice:\s*(\d+)", t)
        k = int(m[-1]) - 1 if m else 0
        return (k if 0 <= k < len(ranked) else 0), bool(m)

    def imagine(self, goal, obs, cands):
        ax, _ = obs_text(obs, self.chars(6000))
        url = obs.get("url", "")
        outcomes = [self.llm.chat([{"role": "user", "content": WM.format(url=url, ax=ax, action=a)}], max_tokens=160,
                                  tag="wm")[0] for a, _ in cands]
        scores = []
        for (a, v), o in zip(cands, outcomes):
            t = self.llm.chat([{"role": "user", "content": VALUE.format(goal=goal, url=url, action=a, outcome=o)}],
                              max_tokens=8, tag="value")[0]
            m = re.search(r"(\d+(?:\.\d+)?)", t)
            scores.append((float(m.group(1)) if m else 5.0) + 0.01 * v)
        best = max(range(len(cands)), key=lambda i: scores[i])
        return cands[best][0], {"wm_scores": scores, "outcomes": [o[:300] for o in outcomes]}

    def form_check(self, goal, obs, action):
        ax, _ = obs_text(obs, self.chars(8000))
        t = self.llm.chat([{"role": "user", "content": FORM_CHECK.format(goal=goal, action=action, ax=ax)}],
                          max_tokens=80, tag="form")[0]
        m = re.search(r"FIX:\s*(.+)", t)
        if m and parse_action(m.group(1).strip())[0] not in ("noop",):
            return m.group(1).strip().split("\n")[0]
        return None

    def progress(self, goal, action, obs0, obs1):
        a0, _ = obs_text(obs0, self.chars(2500))
        a1, _ = obs_text(obs1, self.chars(2500))
        t = self.llm.chat([{"role": "user", "content": PROGRESS.format(goal=goal, action=action, u0=obs0.get("url"),
                                                                        a0=a0, u1=obs1.get("url"), a1=a1)}],
                          max_tokens=4, tag="progress")[0]
        return "yes" in t.lower()

    # ------------------------------------------------------------------ one step
    def step(self, env, goal, obs, history, state):
        """returns (obs, reward, done, step_log). Only the observation, the sampled actions and the reversibility
        PREDICTOR are used for decisions -- never the logged side-effect labels."""
        m, rc = self.m, self.rc
        msgs = self.messages(goal, obs, history, env.action_set.describe(with_long_description=False, with_examples=True))
        acts = self.sample(msgs, m["n"], m["temperature"], f"policy{len(history)}")
        ranked, u = self.vote(acts) if m["n"] > 1 else ([(acts[0], 1)], 0.0)
        axmap = ax_index(obs)
        url = obs.get("url", "")
        top = ranked[0][0]
        rho = self.pred.prob(top, axmap, url, goal) if m.get("race") else None
        log = {"candidates": ranked[:5], "u": u, "rho_top": rho, "mode": "M0", "executed": []}
        use_rho, use_u, use_m1 = m.get("use_rho", True), m.get("use_u", True), m.get("use_m1", True)
        mode = "M0"
        if m.get("mode_all") == "M2":
            mode = "M2"
        elif m.get("uniform_m2"):
            # compute-matched uniform allocation: the SAME extra compute as RACE (M2 + form check) spent on a random
            # share p of the steps, p calibrated so that the expected tokens/step equal RACE's (results/compute_match.json)
            draw = int(hashlib.sha1(f"{self.cfg['seed']}|{goal}|{len(history)}".encode()).hexdigest()[:12], 16) / 16 ** 12
            log["uniform_draw"] = draw
            if draw < float(m.get("m2_prob") or 0.0):
                mode = "M2"
        elif m.get("m1_if_uncertain") and u > rc["tau_u"]:
            mode = "M1"
        elif m.get("arbiter") and u > rc["tau_u"] and len(ranked) > 1:
            k, ok = self.arbiter(goal, obs, history, ranked)
            log.update(arbiter={"choice": k, "parsed": ok})
            top = ranked[k][0]
        elif m.get("race"):
            if use_rho and rho > rc["tau_rho"]:
                mode = "M2"
            elif use_u and u > rc["tau_u"]:
                # without rho every uncertain step is verified by imagination (no knowledge of what is safe to try)
                mode = ("M1" if use_m1 else "M0") if use_rho else "M2"
        log["mode"] = mode
        if mode == "M2":
            cands = ranked[:rc["m2_candidates"]]
            if len(cands) < 2:
                more = self.sample(msgs, 3, 1.0, f"m2more{len(history)}")
                ranked2, _ = self.vote(acts + more)
                cands = ranked2[:rc["m2_candidates"]]
            choice, info = self.imagine(goal, obs, cands)
            log.update(info)
            log["m2_cands"] = [a for a, _ in cands]
            log["m2_choice"] = choice
            if (m.get("race") or m.get("uniform_m2")) and m.get("form_check", True) and state["fixes"] < 2:
                gated = bool(m.get("race")) and use_rho  # uniform_matched: no rho -> every M2 step is form-checked
                r2 = self.pred.prob(choice, axmap, url, goal) if gated else None
                log["rho_choice"] = r2
                # ... except actions that submit nothing (answers, navigation; rho of these is 0 by rule for RACE too)
                submits = parse_action(choice)[0] not in NAVIGATION | {"send_msg_to_user", "report_infeasible"}
                if (r2 > rc["tau_rho"]) if gated else submits:
                    fix = self.form_check(goal, obs, choice)
                    if fix:
                        state["fixes"] += 1
                        log["form_fix"] = fix
                        choice = fix
            return self._execute(env, choice, axmap, url, obs, history, log)
        if mode == "M1":
            cands = [a for a, _ in ranked[:rc["m1_max_tries"]]]
            if m.get("race"):  # RACE only explores candidates that are themselves predicted reversible
                rhos = [rho] + [self.pred.prob(a, axmap, url, goal) for a in cands[1:]]
                log["m1_rhos"] = rhos
                cands = [a for a, r in zip(cands, rhos) if r <= rc["tau_rho"]]
            for j, a in enumerate(cands):
                obs1, r, done, log = self._execute(env, a, axmap, url, obs, history, log)
                if done or j == len(cands) - 1:
                    return obs1, r, done, log
                if self.progress(goal, a, obs, obs1):
                    return obs1, r, done, log
                if obs1.get("url") == url:
                    return obs1, r, done, log  # same URL: nothing to roll back to, keep the result
                log["executed"][-1]["rolled_back"] = True  # server-side effects of this action persist
                obs_b, r, done, log = self._execute(env, "go_back()", ax_index(obs1), obs1.get("url", ""), obs1,
                                                   history, log, rollback=True)
                if done:
                    return obs_b, r, done, log
                obs = obs_b
                axmap = ax_index(obs)
                url = obs.get("url", "")
            return obs, 0.0, False, log
        return self._execute(env, top, axmap, url, obs, history, log)

    def _execute(self, env, action, axmap, url, obs, history, log, rollback=False):
        feat = features(action, axmap, url)
        _, args = parse_action(action)
        el = axmap.get(str(args[0]), {}) if args else {}
        obs1, reward, term, trunc, info, side = env.step(action)
        log["executed"].append({"action": action, "rollback": rollback, "feat": feat, "url": url,
                                "element": {k: el.get(k) for k in ("role", "name", "context", "in_form")}, **side})
        history.append(action + ("  [rolled back]" if rollback else ""))
        return obs1, reward, bool(term or trunc), log
