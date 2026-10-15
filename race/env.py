"""WebArena via BrowserGym + side-effect instrumentation.

* RaceWebArenaTask : BrowserGym's WebArena task, optionally with an overriding task config. For the evaluation splits
                     the WebArena-Lite config (corrected intents / reference answers of 34 of the 134 non-map tasks)
                     replaces the original WebArena annotation that BrowserGym ships.
* NetLogger        : records every HTTP request issued by the browser during an action (method, url, body prefix).
* StateProbe       : server-side state fingerprints of locally hosted sites (`<docker|podman> exec` into the site
                     databases). A changed fingerprint after an action = the action had a server-side side effect.
* label            : DB fingerprint changed (if a working probe exists for the site), else a non-GET request to a
                     WebArena host that is not a known read-only endpoint (GitLab GraphQL counts only for mutations).
                     Labels are only LOGGED; no policy ever reads them (they are used to train the predictor on the
                     disjoint collection split and to compute the IER metrics).
"""
import json
import os
import re
import subprocess
import time
import urllib.parse

from .utils import DATA

# (client executable, client args, [sub-queries]). Every sub-query returns one scalar; the fingerprint of a site is the
# tuple of all of them. Sub-queries are validated once per runner process: one that errors (a table / column that does
# not exist in this image version) is dropped and logged, instead of disabling the whole probe of the site.
SQL = {
    "shopping": ("mysql", ["-u", "magentouser", "-pMyPassword", "magentodb", "-N", "-e"], [
        "SELECT COALESCE(SUM(qty),0) FROM quote_item", "SELECT COUNT(*) FROM quote_item",
        "SELECT COUNT(*) FROM sales_order", "SELECT MAX(updated_at) FROM sales_order", "SELECT COUNT(*) FROM review",
        "SELECT COUNT(*) FROM wishlist_item", "SELECT MAX(updated_at) FROM customer_address_entity",
        "SELECT COUNT(*) FROM customer_address_entity", "SELECT MAX(updated_at) FROM customer_entity",
        "SELECT COUNT(*) FROM newsletter_subscriber", "SELECT COALESCE(SUM(subscriber_status),0) FROM newsletter_subscriber"]),
    "shopping_admin": ("mysql", ["-u", "magentouser", "-pMyPassword", "magentodb", "-N", "-e"], [
        "SELECT MAX(updated_at) FROM catalog_product_entity", "SELECT COUNT(*) FROM catalog_product_entity",
        "SELECT COALESCE(SUM(value),0) FROM catalog_product_entity_decimal",
        "SELECT COALESCE(SUM(qty),0) FROM cataloginventory_stock_item", "SELECT MAX(updated_at) FROM catalog_category_entity",
        "SELECT MAX(updated_at) FROM sales_order", "SELECT COUNT(*) FROM sales_order_status_history",
        "SELECT COUNT(*) FROM sales_invoice", "SELECT COUNT(*) FROM sales_shipment", "SELECT COUNT(*) FROM sales_creditmemo",
        "SELECT COUNT(*) FROM review", "SELECT COALESCE(SUM(status_id),0) FROM review", "SELECT COUNT(*) FROM review_detail",
        "SELECT MAX(update_time) FROM cms_page", "SELECT COUNT(*) FROM cms_page", "SELECT COUNT(*) FROM salesrule",
        "SELECT MAX(updated_at) FROM customer_entity", "SELECT COUNT(*) FROM customer_entity"]),
    "gitlab": ("gitlab-psql", ["-d", "gitlabhq_production", "-t", "-A", "-c"], [
        "select max(updated_at) from projects", "select count(*) from projects", "select count(*) from events",
        "select max(updated_at) from issues", "select count(*) from issues", "select max(updated_at) from merge_requests",
        "select count(*) from notes", "select count(*) from members", "select max(updated_at) from labels",
        "select count(*) from milestones", "select count(*) from users_star_projects", "select max(updated_at) from namespaces",
        "select count(*) from user_statuses", "select md5(coalesce(string_agg(coalesce(message,''), '|' order by user_id),''))"
        " from user_statuses"]),
    "reddit": ("psql", ["-U", "postmill", "-d", "postmill", "-t", "-A", "-c"], [
        "select count(*) from submissions", "select count(*) from comments", "select count(*) from submission_votes",
        "select count(*) from comment_votes", "select count(*) from forums", "select count(*) from forum_subscriptions",
        "select max(edited_at) from submissions", "select max(edited_at) from comments",
        "select count(*) from moderators", "select count(*) from messages",
        "select md5(coalesce(string_agg(coalesce(biography,''), '|' order by id),'')) from users",
        "select md5(coalesce(string_agg(coalesce(description,'')||coalesce(sidebar,''), '|' order by id),'')) from forums"]),
}
CONTAINER = {"shopping": "shopping", "shopping_admin": "shopping_admin", "reddit": "forum", "gitlab": "gitlab"}
# requests that never change persistent, task-relevant server state although they are not GETs: analytics / telemetry,
# search, Magento's "recently viewed" synchronisation (a POST on EVERY product page view), GitLab usage pings
READONLY_PATTERNS = re.compile(r"(preview_markdown|/collect|analytics|autocomplete|/search|customer/section/load|"
                               r"frontend_action_synchronize|usage_data|snowplow|/-/sp\b|/-/manifest|track|beacon|"
                               r"/log\b|/messages/unread|/notifications/count|mixpanel)", re.I)
PLACEHOLDERS = {"__GITLAB__": "gitlab", "__REDDIT__": "reddit", "__SHOPPING__": "shopping",
                "__SHOPPING_ADMIN__": "shopping_admin", "__WIKIPEDIA__": "wikipedia", "__MAP__": "map"}


def probe_sql(subqueries):
    return "SELECT " + ",".join(f"({q})" for q in subqueries)


def probe_cmd(cli, site, subqueries=None):
    exe, args, subs = SQL[site]
    return [cli, "exec", CONTAINER[site], exe, *args, probe_sql(subqueries if subqueries is not None else subs)]


class StateProbe:
    """sites: the sites with a local container (run_all passes RACE_PROBE_SITES); cli: docker | podman.
    The first snapshot of a site validates its sub-queries; failing ones are dropped (`dropped`, logged per episode).
    A site whose probe keeps failing (container down) is marked `broken` -> network-heuristic labels for that site."""

    def __init__(self, sites=(), cli="docker", runner=None):
        self.sites, self.cli = set(sites), cli
        self.broken = set()
        self.active = {}    # site -> validated sub-queries
        self.dropped = {}   # site -> sub-queries removed because they error in this image
        self._run = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30))

    @classmethod
    def from_env(cls):
        sites = [s for s in os.environ.get("RACE_PROBE_SITES", "").split(",") if s]
        return cls(sites, os.environ.get("RACE_CONTAINER_CLI") or "docker")

    def _exec(self, site, subs, tries=2):
        for attempt in range(tries):  # one retry: a single slow `exec` under load must not disable the probe
            try:
                r = self._run(probe_cmd(self.cli, site, subs))
                if r.returncode == 0:
                    return r.stdout.strip()
            except Exception:  # CLI missing / container down / timeout
                pass
            if attempt + 1 < tries:
                time.sleep(2)
        return None

    def _validate(self, site):
        subs = SQL[site][2]
        if self._exec(site, subs) is not None:
            self.active[site] = list(subs)
            return True
        ok = [q for q in subs if self._exec(site, [q], tries=1) is not None]
        if not ok:
            return False  # nothing works: the container (not the SQL) is the problem
        self.active[site] = ok
        self.dropped[site] = [q for q in subs if q not in ok]
        return True

    def snapshot(self, sites):
        out = {}
        for s in sites:
            if s not in self.sites or s not in SQL or s in self.broken:
                continue
            if s not in self.active and not self._validate(s):
                self.broken.add(s)
                continue
            v = self._exec(s, self.active[s])
            if v is None:
                self.broken.add(s)  # for the rest of this runner process: network-heuristic labels (logged)
            else:
                out[s] = v
        return out

    def describe(self):
        return {"active": {s: len(q) for s, q in self.active.items()}, "dropped": self.dropped,
                "broken": sorted(self.broken)}


class NetLogger:
    def __init__(self):
        self.reqs = []
        self.server_errors = []

    def attach(self, context):
        def on_req(req):
            try:
                body = (req.post_data or "")[:400]
            except Exception:
                body = ""
            self.reqs.append((req.method, req.url, body))

        def on_resp(resp):  # 5xx on a page load = the site (not the agent) failed
            try:
                if resp.status >= 500 and resp.request.resource_type == "document":
                    self.server_errors.append((resp.status, resp.url))
            except Exception:
                pass
        context.on("request", on_req)
        context.on("response", on_resp)

    def pop(self):
        r, self.reqs = self.reqs, []
        return r

    def pop_errors(self):
        r, self.server_errors = self.server_errors, []
        return r


# login pages (session expired / logged out): GitLab, Postmill, Magento storefront; the Magento admin login form is
# shown under the admin URL itself and is recognised by its page title
LOGIN_URL = re.compile(r"(/users/sign_in|/login(?:$|[/?#])|/customer/account/login)", re.I)
OFFSITE_GOTO = re.compile(r"""\s*goto\(\s*(?:url\s*=\s*)?["']([^"']*)["']""")
ERROR_TITLE = re.compile(r"(502 Bad Gateway|503 Service|504 Gateway|Gateway Time-?out|Internal Server Error|"
                         r"There has been an error processing your request|We're sorry, but something went wrong)", re.I)


def heuristic_state_change(reqs, hosts):
    for method, url, body in reqs:
        if method in ("GET", "HEAD", "OPTIONS"):
            continue
        host = urllib.parse.urlparse(url).netloc
        if host not in hosts or READONLY_PATTERNS.search(url):
            continue
        if "graphql" in url and "mutation" not in body:
            continue
        return True
    return False


def task_tables():
    lite = json.load(open(DATA / "test_webarena_lite.raw.json", encoding="utf-8"))
    full = json.load(open(DATA / "webarena_test.raw.json", encoding="utf-8"))
    return lite, {t["task_id"]: t for t in full}


def lite_configs():
    """old (WebArena) task id -> WebArena-Lite config (with the original task id, as BrowserGym expects)"""
    lite, _ = task_tables()
    return {t["old_task_id"]: {**t, "task_id": t["old_task_id"], "lite_task_id": t["task_id"]} for t in lite}


def obs_text(obs, max_chars):
    from browsergym.utils.obs import flatten_axtree_to_str
    ax = flatten_axtree_to_str(obs["axtree_object"])
    if len(ax) > max_chars:
        ax = ax[:max_chars] + "\n... (truncated)"
    tabs = obs.get("open_pages_urls") or []
    return ax, tabs


def ax_index(obs):
    """bid -> {role, name, ancestors (roles), in_form}"""
    nodes = obs["axtree_object"]["nodes"]
    by_id = {n["nodeId"]: n for n in nodes}
    parent = {}
    for n in nodes:
        for c in n.get("childIds", []):
            parent[c] = n["nodeId"]
    out = {}
    for n in nodes:
        bid = n.get("browsergym_id")
        if bid is None:
            continue
        anc, cur, names = [], n["nodeId"], []
        for _ in range(8):
            cur = parent.get(cur)
            if cur is None or cur not in by_id:
                break
            p = by_id[cur]
            anc.append((p.get("role") or {}).get("value", ""))
            nm = (p.get("name") or {}).get("value", "")
            if nm:
                names.append(nm)
        out[str(bid)] = {"role": (n.get("role") or {}).get("value", ""), "name": (n.get("name") or {}).get("value", ""),
                         "ancestors": anc, "context": " ".join(names[:3])[:200],
                         "in_form": any(a in ("form", "dialog", "alertdialog") for a in anc)}
    return out


def _task_class():
    from browsergym.webarena.task import GenericWebArenaTask

    class RaceWebArenaTask(GenericWebArenaTask):
        def __init__(self, seed, task_id, override_config=None, **kw):
            super().__init__(seed, task_id=task_id, **kw)
            if override_config is not None:
                s = json.dumps(override_config)
                for pat, key in PLACEHOLDERS.items():
                    s = s.replace(pat, self.webarena_instance.urls[key])
                self.task_configs = [json.loads(s)]

        def setup(self, page):
            if os.environ.get("RACE_SMOKE_NO_LOGIN") == "1":  # local smoke test against a mock site only
                self.webarena_instance.ui_login = lambda site, page: None
            return super().setup(page)

    return RaceWebArenaTask


class WebEnv:
    """thin wrapper: one BrowserGym env per task.

    Side-effect labelling per executed action: DB fingerprint (or mutating-request heuristic) before vs. after the
    action, after a short network-idle wait (AJAX submissions). A change that only shows up later (between this
    action's after-snapshot and the next action's before-snapshot, or at the end of the episode) is recorded
    separately as `delayed` (it may also be background noise such as cron jobs) and is reported as a sensitivity
    analysis, not merged into the main label.
    Robustness: 5xx page loads / error pages are flagged (`server_error`), and a redirect to a login page (expired
    session) triggers one re-login with BrowserGym's own login routine and a return to the previous page."""

    def __init__(self, task_id, cfg, probe, override_config=None):
        from . import compat  # noqa: F401  (must precede any webarena import)
        from browsergym.core.action.highlevel import HighLevelActionSet
        from browsergym.core.env import BrowserEnv
        from browsergym.core.registration import frozen_partial
        self.action_set = HighLevelActionSet(subsets=["chat", "infeas", "bid", "nav", "tab"], strict=False,
                                             multiaction=False)
        entry = frozen_partial(_task_class(), task_id=task_id, override_config=override_config)
        self.env = BrowserEnv(entry, headless=True, action_mapping=self.action_set.to_python_code)
        self.net = NetLogger()
        self.probe = probe
        self.cfg = cfg
        self.hosts = set()
        self.sites = []
        self.settle_ms = int(cfg.get("webarena", {}).get("settle_ms", 1500))
        self.max_relogins = int(cfg.get("webarena", {}).get("max_relogins", 2))
        self.last_after, self.last_title, self.n_actions = None, "", 0
        self.delayed, self.relogins = {}, []

    def reset(self, sites):
        obs, info = self.env.reset(seed=self.cfg["seed"])
        self.sites = sites
        self.net.attach(self.env.context)
        inst = self.env.task.webarena_instance
        self.hosts = {urllib.parse.urlparse(u).netloc for u in inst.urls.values()}
        self.allowed = self.hosts | {urllib.parse.urlparse(inst.home_url).netloc}
        self._settle()
        self.net.pop()  # drop login / start-page traffic
        self.net.pop_errors()
        self.last_title = self._title()
        return obs

    # ------------------------------------------------------------------ helpers
    def _settle(self):
        if self.settle_ms <= 0:
            return
        try:
            self.env.page.wait_for_load_state("networkidle", timeout=self.settle_ms)
        except Exception:  # long-polling pages never become idle: the wait is only a bounded grace period
            pass

    def _title(self):
        try:
            return self.env.page.title() or ""
        except Exception:
            return ""

    def _login_site(self, url, title, prev_url, prev_title):
        """site whose login page we were redirected to (None if not a login page / already on one before)"""
        try:
            urls = self.env.task.webarena_instance.urls
        except Exception:
            return None
        loc = urllib.parse.urlparse(url or "")
        for site in self.sites:
            base = urls.get(site) or ""
            if not base or urllib.parse.urlparse(base).netloc != loc.netloc:
                continue
            if site == "shopping_admin":
                is_login, was_login = title.strip() == "Magento Admin", prev_title.strip() == "Magento Admin"
            else:
                is_login = bool(LOGIN_URL.search(loc.path))
                was_login = bool(LOGIN_URL.search(urllib.parse.urlparse(prev_url or "").path))
            if is_login and not was_login:
                return site
        return None

    def _relogin(self, site, back_url):
        try:
            self.env.task.webarena_instance.ui_login(site=site, page=self.env.page)
            if back_url:
                self.env.page.goto(back_url)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ step
    def step(self, action):
        before = self.probe.snapshot(self.sites)
        late = self.net.pop()  # requests issued after the previous after-snapshot
        if self.n_actions and self.last_after is not None:
            if any(s in self.last_after and before[s] != self.last_after[s] for s in before):
                self.delayed[self.n_actions - 1] = "db"
            elif heuristic_state_change(late, self.hosts):
                self.delayed[self.n_actions - 1] = "network"
        try:
            url0 = self.env.page.url
        except Exception:
            url0 = ""
        # BrowserGym terminates the whole task (reward 0) as soon as a tab is on a non-WebArena host; the LLM tends to
        # goto('https://www.reddit.com/...') on Postmill -> reject such a goto as an invalid action instead
        m = OFFSITE_GOTO.match(action or "")
        blocked = m and urllib.parse.urlparse(m.group(1)).netloc not in self.allowed | {""}
        obs, reward, terminated, truncated, info = self.env.step("noop()" if blocked else action)
        if blocked:
            obs["last_action_error"] = (f"goto('{m.group(1)}') was not executed: external websites are not "
                                        f"reachable. Stay on this website ({url0 or 'current site'}) and use its "
                                        f"own links, menus and search.")
        self._settle()
        after = self.probe.snapshot(self.sites)
        reqs = self.net.pop()
        both = [s for s in before if s in after]
        net = heuristic_state_change(reqs, self.hosts)
        unprobed = [s for s in self.sites if s not in both]
        changed = any(before[s] != after[s] for s in both) or (net if unprobed else False)
        src = "db" if not unprobed else ("network" if not both else "db+network")
        title = self._title()
        errs = self.net.pop_errors()
        side = {"state_changed": bool(changed), "label_source": src, "net_mutation": net, "n_requests": len(reqs),
                "server_error": bool(errs) or bool(ERROR_TITLE.search(title))}
        if errs:
            side["http_5xx"] = errs[:3]
        self.n_actions += 1
        self.last_after = after
        if not (terminated or truncated) and len(self.relogins) < self.max_relogins:
            site = self._login_site(obs.get("url", ""), title, url0, self.last_title)
            if site:
                ok = self._relogin(site, url0)
                self.relogins.append(site if ok else f"{site}:failed")
                side["relogin"] = self.relogins[-1]
                if ok:
                    obs, reward, terminated, truncated, info = self.env.step("noop()")
                    self._settle()
                    self.last_after = self.probe.snapshot(self.sites)  # login traffic is not the action's effect
                    self.net.pop()
                    self.net.pop_errors()
                    title = self._title()
        self.last_title = title
        return obs, reward, terminated, truncated, info, side

    def finish(self):
        """end-of-episode flush: delayed effects of the LAST action -> {env action index: 'db' | 'network'}"""
        try:
            if self.n_actions and self.last_after is not None:
                self._settle()
                snap = self.probe.snapshot(self.sites)
                late = self.net.pop()
                if any(s in self.last_after and snap[s] != self.last_after[s] for s in snap):
                    self.delayed[self.n_actions - 1] = "db"
                elif heuristic_state_change(late, self.hosts):
                    self.delayed[self.n_actions - 1] = "network"
        except Exception:
            pass
        return dict(self.delayed)

    def close(self):
        try:
            self.env.close()
        except Exception:
            pass
