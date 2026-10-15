"""OpenAI-compatible chat client (local vLLM server) with token accounting and an on-disk cache.

* Every call adds (prompt, completion) token counts to a Meter, so each method's compute is measured.
* Cache: one sqlite file per (model, lane) -- DATA/cache/<model>/<lane>.sqlite -- so parallel lanes never share a file
  (sqlite on NFS: journal_mode DELETE, never WAL). Identical prompts of different methods within a lane hit the same
  entry (common random numbers: methods that share a prefix of decisions see identical samples), and interrupted runs
  are deterministic on resume.
* Server down after retries -> LLMUnavailable (the runner exits with code 75 and run_all restarts the server).
* Prompt longer than the context window -> the longest message is truncated and the call retried (logged).
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time

from .utils import DATA, DEBUG, slug


class LLMUnavailable(RuntimeError):
    pass


class Meter:
    """totals + a breakdown by call purpose (tag without its step number: policy, m2more, wm, value, form, progress,
    arbiter, rho) -> per-component cost accounting of every method"""

    def __init__(self):
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.by_tag = {}
        self.cache_hits = 0  # cached calls still count their stored usage (= compute), but cost no wall time

    def add(self, pt, ct, n_calls=1, tag=""):
        self.calls += n_calls
        self.prompt_tokens += pt
        self.completion_tokens += ct
        k = re.sub(r"\d+$", "", tag or "") or "other"
        v = self.by_tag.setdefault(k, [0, 0, 0])
        v[0] += n_calls
        v[1] += pt
        v[2] += ct

    def snapshot(self):
        return {"calls": self.calls, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens}

    def tags(self):
        return {k: list(v) for k, v in self.by_tag.items()}


def base_url(cfg):
    return os.environ.get("RACE_LLM_BASE_URL") or cfg["llm"]["base_url"]


def max_model_len(cfg):
    return int(os.environ.get("RACE_MAX_MODEL_LEN") or cfg["llm"]["max_model_len"])


class LLMClient:
    def __init__(self, cfg, model=None, seed=None, lane=None):
        from openai import OpenAI
        lc = cfg["llm"]
        self.model = model or lc["model"]
        self.seed = seed  # different seeds -> independent samples (run-to-run variance experiments)
        self.client = OpenAI(base_url=base_url(cfg), api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
                             timeout=lc.get("request_timeout", 300), max_retries=0)
        self.lane = lane or os.environ.get("RACE_LANE") or "main"
        # debug runs (results_debug/, own run_all lock) must never write the sqlite files of the main run
        p = DATA / ("cache_debug" if DEBUG else "cache") / slug(self.model) / f"{slug(self.lane)}.sqlite"
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = self._open_cache(p)
        self.cache_errors = 0
        self.lock = threading.Lock()
        self.meter = Meter()
        self.truncations = 0

    @staticmethod
    def _open_cache(p):
        """a corrupt cache file (e.g. written from two hosts at once over NFS) is moved aside, never fatal"""
        for attempt in range(2):
            try:
                db = sqlite3.connect(str(p), check_same_thread=False, timeout=600)
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, v TEXT)")
                chk = db.execute("PRAGMA quick_check").fetchone()[0]
                if chk == "ok":
                    return db
                db.close()
                err = chk
            except sqlite3.DatabaseError as e:
                err = e
                if not re.search(r"malformed|not a database|corrupt", str(e), re.I):
                    break  # locked / I/O error: transient, keep the file
            if attempt or not p.exists():
                break
            bad = p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            p.rename(bad)
            print(f"[llm] cache {p} is corrupt ({err}) -> moved to {bad.name}, starting an empty cache", flush=True)
        print(f"[llm] cache {p} unusable ({err}) -> running without cache", flush=True)
        return None

    def _cache_error(self, op, e):
        self.cache_errors += 1
        if self.cache_errors <= 3:
            print(f"[llm] cache {op} failed ({type(e).__name__}: {e}) -> ignored (cache miss)", flush=True)

    def _key(self, messages, n, temperature, max_tokens, tag):
        return hashlib.sha1(json.dumps([self.model, messages, n, temperature, max_tokens, tag] +
                                       ([self.seed] if self.seed is not None else [])).encode()).hexdigest()

    @staticmethod
    def _truncate(messages, frac=0.7):
        i = max(range(len(messages)), key=lambda j: len(messages[j]["content"]))
        c = messages[i]["content"]
        cut = int(len(c) * frac)
        out = [dict(m) for m in messages]
        out[i]["content"] = c[:cut] + "\n... (truncated)"
        return out

    def chat(self, messages, n=1, temperature=0.0, max_tokens=512, tag=""):
        """-> list of n strings. Cached results still count their (stored) usage in the meter."""
        import openai
        k = self._key(messages, n, temperature, max_tokens, tag)
        row = None
        if self.db is not None:
            try:
                with self.lock:
                    row = self.db.execute("SELECT v FROM c WHERE k=?", (k,)).fetchone()
            except sqlite3.Error as e:
                self._cache_error("read", e)
        if row:
            v = json.loads(row[0])
            self.meter.add(*v["usage"], tag=tag)
            self.meter.cache_hits += 1
            return v["texts"]
        msgs, last = messages, None
        for attempt in range(8):
            try:
                kw = {"seed": self.seed} if self.seed is not None else {}
                r = self.client.chat.completions.create(model=self.model, messages=msgs, n=n,
                                                        temperature=temperature, max_tokens=max_tokens, **kw)
                break
            except openai.BadRequestError as e:  # almost always: prompt + max_tokens > context window
                if not re.search(r"context|token|length", str(e), re.I) or attempt >= 4:
                    raise
                msgs = self._truncate(msgs)
                self.truncations += 1
                last = e
            except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError,
                    openai.NotFoundError, openai.RateLimitError) as e:
                last = e
                if attempt >= 5:
                    raise LLMUnavailable(f"{type(e).__name__}: {e}") from e
                time.sleep(10 * (attempt + 1))
        else:
            raise LLMUnavailable(f"giving up: {last}")
        texts = [c.message.content or "" for c in r.choices]
        u = r.usage
        usage = [getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0]
        self.meter.add(*usage, tag=tag)
        if self.db is not None:
            try:
                with self.lock:
                    self.db.execute("INSERT OR REPLACE INTO c VALUES (?, ?)",
                                    (k, json.dumps({"texts": texts, "usage": usage})))
                    self.db.commit()
            except sqlite3.Error as e:
                self._cache_error("write", e)
                try:
                    self.db.rollback()
                except sqlite3.Error:
                    pass
        return texts
