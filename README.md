<div align="center">

# Going Back Is Not Undo

### Irreversible side effects of web agents, and a controlled test of reversibility-aware test-time compute

![python](https://img.shields.io/badge/python-3.12-3A6BB0)
![benchmark](https://img.shields.io/badge/benchmark-WebArena--Lite%20(134%20tasks)-2B9C8A)
![episodes](https://img.shields.io/badge/episodes-1%2C923-C99A1E)
![actions](https://img.shields.io/badge/labelled%20actions-10%2C441-D9663F)
![backbone](https://img.shields.io/badge/backbone-Qwen2.5--32B--AWQ-8A56A6)

</div>

<p align="center"><img src="analysis/figures/teaser.png" width="100%" alt="(a) a logged ReAct episode in which three clicks on a link named Reorder each add server-side cart rows; (b) every RACE step by vote uncertainty and predicted irreversibility; (c) the share of side effects each check point catches"></p>

LLM web agents click buttons that change server state: an order is placed, a post is published, a project is
created. The browser's `go_back` restores the page, not the database. This repository contains

* a **measurement instrument** that labels every executed agent action with whether it changed the website's
  database (per-action DB fingerprints of four self-hosted WebArena sites, cross-checked against HTTP traffic);
* **RACE** (Reversibility-Aware Compute allocation for wEb agents), an allocator derived from a per-step cost model:
  plain execution, try-and-backtrack in the live site, or imagine-and-verify with an LLM world model and a form check,
  chosen by vote uncertainty *u* and predicted irreversibility *ρ*;
* seven comparison strategies, including a compute-matched control, and **all 1,923 episode logs** of the study,
  from which every number below can be recomputed without a GPU.

---

## Findings at a glance

| | What we measured | Result |
|---|---|---|
| **1** | How often do agents change server state? | 202 of 10,441 executed actions (1.9 %). 198 of them (98 %) are clicks on a control whose accessible name contains a mutating verb (*Create submission*, *Reorder*, *Subscribe*, *Upvote*, …). The other 9,579 actions cause 4. |
| **2** | Can a change be caught before it lands? | 201 of 202 changes issue a non-GET request. Holding non-GET requests intercepts 3.7 % of actions and **99.5 %** of side effects. At about the same rate, a keyword rule catches 78.2 % and the learned page-level predictor 41.6 %; neither catches any of the 35 changes triggered by clicking a *link*. |
| **3** | Does more test-time compute make agents safer? | Seven strategies spending 1.05–2.32× ReAct's tokens, RACE included, leave success (21.6–28.4 %) and the irreversible-error rate (3.7–8.2 %) statistically unchanged: no paired difference survives Holm correction (p ≥ 0.875). |
| **4** | Why not? | Most irreversible errors are intended commits with wrong or incomplete content, or commits repeated because the page gave no feedback. The world model announced 34 of the 45 side effects it was asked to foresee, and the agent executed them anyway. |
| **5** | How large must an evaluation be? | A second sampling seed moves RACE's success by 8.8 points on the same 68 tasks. At the observed 10 % discordance, detecting a 3-point difference with 80 % power needs about 870 paired tasks; WebArena-Lite has 165. |

<p align="center"><img src="analysis/figures/outcomes.png" width="100%" alt="success rate and irreversible-error rate with 95% bootstrap intervals for the eight strategies, and the anatomy of the irreversible errors"></p>

---

## Main results

WebArena-Lite, the 134 tasks that do not need the map site (72 read-only, 62 state-changing), Qwen2.5-32B-Instruct-AWQ
as policy, world model, value function, checker and fuzzy-match judge. SR / Safe-SR / IER in %.

| Strategy | Tokens / task | SR ↑ | Safe-SR ↑ | IER ↓ | IER read-only | IER failed-change | Explored action changed state |
|---|---:|---:|---:|---:|---:|---:|---:|
| ReAct | 43.6k (1.00×) | 24.6 | 24.6 | 5.2 | 2.8 | 8.1 | 0.0 |
| SC-vote3 | 46.1k (1.06×) | 22.4 | 21.6 | 6.7 | 5.6 | 8.1 | 0.0 |
| CATTS (re-impl.) | 46.6k (1.07×) | 26.1 | 26.1 | 6.7 | 1.4 | 12.9 | 0.0 |
| WM-all (WebDreamer-style) | 101.1k (2.32×) | **28.4** | **27.6** | 5.2 | 2.8 | 8.1 | 0.0 |
| Tryback-all | 50.3k (1.15×) | 23.9 | 23.9 | 8.2 | 2.8 | 14.5 | 3.0 |
| Confirm-rule | 45.9k (1.05×) | 23.9 | 23.9 | 5.2 | 1.4 | 9.7 | 0.0 |
| Uniform-matched | 54.6k (1.25×) | 21.6 | 21.6 | **3.7** | 1.4 | 6.5 | 0.0 |
| **RACE** | 54.7k (1.25×) | 24.6 | 23.9 | 6.7 | 4.2 | 9.7 | 2.2 |

All 95 % bootstrap intervals overlap. Uniform-matched spends RACE's token budget (achieved difference +0 %,
95 % CI [−10, +13]) on randomly chosen steps. Full tables, paired tests, the ablations, threshold sensitivity, the
second seed, the 14B backbone and per-site numbers are in [`results/summary.md`](results/summary.md).

<table>
<tr>
<td width="58%"><img src="analysis/figures/where.png" alt="probability of a server change by kind of action, and the controls behind the 202 changes"></td>
<td width="42%"><img src="analysis/figures/predict.png" alt="side effects caught versus share of actions held, for the learned predictor, the keyword rule and the HTTP gate"></td>
</tr>
<tr>
<td align="center"><sub>Where the 202 changes come from</sub></td>
<td align="center"><sub>Page-level predictors vs. holding non-GET requests</sub></td>
</tr>
</table>

---

## How side effects are measured

Each site runs as a container from the WebArena-Verified images. Before and after every executed action,
[`race/env.py`](race/env.py) reads a **fingerprint** of the site database through `docker exec`: 11–18 scalar
sub-queries per site (row counts, sums and latest update timestamps of cart items, orders, invoices, wish lists,
reviews, newsletter subscriptions, prices and stock, CMS pages, issues, merge requests, members, stars, user statuses,
submissions, comments, votes, forum subscriptions, messages). An action is labelled *state-changing* if a fingerprint
differs after a 1.5 s network-idle grace period. Every request the page issues is logged as well.

| Check | Value |
|---|---|
| Navigation actions labelled state-changing (false-positive floor) | 0.1 % of 1,887 |
| Agreement of DB labels and network labels (collection split) | 98.1 % of 1,140 actions |
| DB changes that coincide with a non-GET request (evaluation runs) | 201 of 202 |

Task-level metrics, with *c(e)* = the episode changed server state:

* **IER** (irreversible-error rate): share of episodes with *c(e)* and either a read-only task or a failed
  state-changing task, i.e. the server was left in a state nobody asked for, including a half-finished one;
* **Safe-SR**: successes without such a change;
* **Explored action changed state**: an action that the agent later backed out of (`go_back`) had changed state.

The policy never sees the labels; they are used for the metrics and for training the predictor.

---

## RACE in one step

```
votes, u   <- 3 policy samples, normalised vote entropy
rho        <- P(top action changes server state)            # logistic regression on page features
if rho > 0.5:                                                # M2  imagine & verify
    world model describes each of the top-3 candidates, a value prompt scores them,
    a form check compares the submitted fields with the goal (may substitute one corrective fill/select)
elif u > 0.5:                                                # M1  try & backtrack
    execute up to 2 candidates with rho <= 0.5; go_back if no progress and the URL changed
else:                                                        # M0  execute the top vote
```

The predictor ([`race/reversibility.py`](race/reversibility.py)) is trained on 1,140 database-labelled actions
collected on 103 WebArena tasks that are not in WebArena-Lite and share none of its intent templates. All prompts are
in [`race/agent.py`](race/agent.py).

---

## Repository layout

```
.
├── run_all.py                 one command for every stage (preflight → collect → predictor → main → ablations → aggregate)
├── configs/default.yaml       models, methods, thresholds, budgets
├── race/                      the package
│   ├── env.py                 BrowserGym wrapper, DB fingerprint probe, HTTP logger, re-login, off-site guard
│   ├── agent.py               voting, M0 / M1 / M2, CATTS arbiter, uniform-matched allocation, prompts
│   ├── reversibility.py       features, keyword rule, learned predictor, zero-shot LLM judgement
│   ├── runner.py              task splits, episode loop, retries, resumable per-episode JSON
│   ├── compute_match.py       calibration of the compute-matched control
│   ├── judge.py, rejudge.py   WebArena's fuzzy-match judge served by the local model
│   ├── aggregate.py           metrics, paired tests (exact McNemar, bootstrap, Holm), tables, figures, case dumps
│   └── infra.py, llm.py, runtime.py, utils.py, compat.py
├── scripts/                   WebArena deployment / reset, offline download helpers, offline self-test
├── data/                      WebArena and WebArena-Lite task tables, trained predictor (reversibility_model.pkl)
├── results/
│   ├── eval/<strategy>/<task>.json        main comparison, 8 × 134 episodes
│   ├── ablation/<variant>/<task>.json     ablations, sensitivity, second seed, 14B backbone, 11 × 68 episodes
│   ├── collect/tryback_all/<task>.json    predictor-training split, 103 episodes
│   ├── summary.md, metrics.json           everything aggregated
│   ├── latex/, figures/, cases/           generated tables, plots, win/loss and error case dumps
│   ├── compute_match.json, reversibility_eval.json, env_info.json
│   └── annotation/side_effect_labels.csv  automatic labels of 126 sampled actions (human_label column left for checking)
└── analysis/                  study-level analyses and the figures shown above
    ├── analysis.py            → stats.json, wm_foresight_coding.csv
    └── figures.py             → figures/*.pdf|png
```

---

## Reproduce the numbers from the logs (no GPU, no WebArena)

```bash
pip install numpy scipy scikit-learn matplotlib pyyaml
python -m race.aggregate          # results/summary.md, metrics.json, latex/, figures/, cases/
python analysis/analysis.py       # analysis/stats.json (side-effect census, predictors, HTTP gate, power)
python analysis/figures.py        # analysis/figures/*
python scripts/selftest.py        # whole pipeline on a fake site with a fake LLM (a few minutes, temp copy)
```

Each episode file records the task, every step (sampled candidates and votes, *u*, *ρ*, mode, world-model outcomes
and scores, form-check fixes), every executed action (target element, URL, `state_changed`, `net_mutation`, number of
requests, rollback flag), token counts per component, the evaluator's scores and the final answer.

---

## Run the experiments

**Requirements.** Linux, two 24 GB GPUs (the study used two RTX A5000), vLLM pre-installed, Docker or rootless podman
for the four WebArena sites (about 60 GB of container storage for the WebArena-Verified images), ≥ 32 GB RAM.

```bash
python run_all.py --preflight_only          # checks packages, headless Chromium, WebArena, model weights; no GPU
CUDA_VISIBLE_DEVICES=0,1 nohup python run_all.py > run.log 2>&1 &
```

* `run_all.py` installs missing packages in stages and never re-resolves vLLM (do not `pip install -r
  requirements.txt` next to an installed vLLM). It deploys the sites with `scripts/setup_webarena.sh`, starts a vLLM
  server for `Qwen/Qwen2.5-32B-Instruct-AWQ`, and re-creates the site containers before every (strategy, site group)
  job. Read-only tasks run before state-changing ones.
* **Remote WebArena** (no container access on the GPU host): `export WA_HOST=<host>` and
  `export WA_FULL_RESET=http://<host>:7565`. Side-effect labels then fall back to the network heuristic and are
  marked `label_source: network`.
* **Resumable.** Every episode is one atomically written JSON file; re-running the command only runs missing tasks.
  Environment failures are retried and saved as `env_failure` after three attempts (two episodes in this study).
* `--download_only` fetches packages, Chromium, NLTK data, model weights and site images without a GPU;
  `--max_tasks N` runs a small debug pass into `results_debug/`.
* Exit codes: 0 finished, 1 a stage is incomplete (re-run to resume), 2 preflight failed.

The study ran 1,923 episodes (103 collection, 1,072 main, 748 ablation / sensitivity / seed / backbone), about 42.5
episode-hours of browser time, with the site groups running in parallel against one shared vLLM server.

---

## Notes on the data

* **Judge.** WebArena's fuzzy-match and unachievable-task checks use the served Qwen2.5-32B model instead of GPT-4,
  also for the 14B runs (re-judged afterwards). Absolute success rates are therefore not comparable to GPT-4-judged
  numbers on the original images.
* **Environment.** Sites run from the WebArena-Verified images v0.1.0 (same site data and DB credentials as the
  original images, with upstream fixes). The map site is not deployed, so map tasks are excluded.
* **Audited tasks.** In task 641 the post is published as requested but the open-model judge grades it "partially
  correct"; task 791 creates a folder and a file although its evaluator type marks it read-only. Together they cause
  14 of the 64 irreversible-error episodes. Removing both lowers every IER by 0.7–1.5 points and leaves the ranking of
  strategies unchanged (`analysis/stats.json`, key `ier_audited`).
* **Probe coverage.** The probes cover the tables that WebArena's state-changing tasks modify, not every table; e.g.
  an order-address edit is not visible to them.
* **HTTP gate.** Its coverage is computed from the logged requests of the runs; no agent was run with the gate active.
* **World-model foresight.** `analysis/wm_foresight_coding.csv` holds the coding of every imagined-then-executed
  action; the 45 that changed state were read by hand (34 announced, 5 described as navigation, 6 "element not found").
* **Human labels.** `results/annotation/side_effect_labels.csv` ships with the automatic labels only; the
  `human_label` column has not been filled in.

---

## Acknowledgements

Built on [WebArena](https://github.com/web-arena-x/webarena), [WebArena-Verified](https://github.com/ServiceNow/webarena-verified),
[BrowserGym](https://github.com/ServiceNow/BrowserGym), [vLLM](https://github.com/vllm-project/vllm) and the
[Qwen2.5](https://github.com/QwenLM/Qwen2.5) models. Icons in the overview figure are from [Icons8](https://icons8.com).

## Citation

The accompanying paper is under anonymous review; citation information will be added after the review period.
