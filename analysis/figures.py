"""All study figures, drawn with matplotlib from stats.json (analysis.py) and the raw episode logs.

python analysis/figures.py   ->  analysis/figures/*.pdf (+ .png previews)
Icons in analysis/icons are from Icons8 (https://icons8.com, "Fluency" set).

Style: one validated categorical palette for every chart (checked with the dataviz validator for lightness band,
chroma floor, colour-blind separation and normal-vision separation), alternating gray/white bands behind the data,
recessive axes, direct labels where they fit.
"""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.offsetbox import AnnotationBbox, OffsetImage  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Patch, Rectangle  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # repository root
FIG = HERE / "figures"
ICON = HERE / "icons"
FIG.mkdir(exist_ok=True)
S = json.load(open(HERE / "stats.json", encoding="utf-8"))

# ---------------------------------------------------------------------------------------------- palette
# categorical slots (validated together: all checks pass in light mode; gold needs direct labels, which it gets)
BLUE, CORAL, TEAL, GOLD, PLUM = "#3A6BB0", "#D9663F", "#2B9C8A", "#C99A1E", "#8A56A6"
# tints of the same hues for fills and regions
BLUE_T, CORAL_T, TEAL_T, GOLD_T, PLUM_T = "#DCE6F4", "#F8E0D7", "#D5EEEA", "#F5EBCF", "#EADDF1"
# sequential ramp (one hue, light -> dark) for ordered magnitudes
SEQ = ["#9DB9DE", "#3A6BB0", "#1F3D6B"]
INK, INK2, MUTED = "#22262B", "#55595F", "#8E9196"
BAND, WHITE, RULE = "#EFEFEC", "#FFFFFF", "#D6D6D2"

plt.rcParams.update({
    "font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"], "font.size": 7,
    "axes.titlesize": 7.5, "axes.titleweight": "bold", "axes.labelsize": 7, "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5, "legend.fontsize": 6.3, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
    "xtick.color": INK2, "ytick.color": INK2, "text.color": INK, "axes.spines.top": False,
    "axes.spines.right": False, "axes.linewidth": 0.6, "xtick.major.width": 0.6, "ytick.major.width": 0.6,
    "xtick.major.size": 2.5, "ytick.major.size": 2.5, "axes.grid": False, "pdf.fonttype": 42, "ps.fonttype": 42,
    "savefig.dpi": 300, "axes.titlepad": 5,
})
NAMES = {"react": "ReAct", "sc_vote3": "SC-vote3", "catts": "CATTS", "wm_all": "WM-all", "tryback_all": "Tryback-all",
         "confirm_rule": "Confirm-rule", "race": "RACE", "uniform_matched": "Uniform-matched"}
ORDER = ["react", "sc_vote3", "catts", "wm_all", "tryback_all", "confirm_rule", "uniform_matched", "race"]


# ---------------------------------------------------------------------------------------------- helpers
def icon(ax, name, xy, zoom=0.16, zorder=6):
    img = plt.imread(ICON / f"fluency_{name}.png")
    ax.add_artist(AnnotationBbox(OffsetImage(img, zoom=zoom), xy, frameon=False, zorder=zorder,
                                 box_alignment=(.5, .5)))


def box(ax, x, y, w, h, fc, ec, lw=0.7, r=0.012, z=1, ls="-"):
    p = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=fc, ec=ec, lw=lw, zorder=z, ls=ls)
    ax.add_patch(p)
    return p


def arrow(ax, a, b, color=INK2, lw=0.8, ls="-", rad=0.0, z=4, ms=6, style="-|>"):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle=style, mutation_scale=ms, color=color, lw=lw, ls=ls, zorder=z,
                                 connectionstyle=f"arc3,rad={rad}", shrinkA=0, shrinkB=0))


def row_bands(ax, n, horizontal=True, start=0):
    """alternating gray / white bands behind categorical rows (horizontal) or columns"""
    for i in range(n):
        c = BAND if (i + start) % 2 == 0 else WHITE
        if horizontal:
            ax.axhspan(i - 0.5, i + 0.5, color=c, lw=0, zorder=0)
        else:
            ax.axvspan(i - 0.5, i + 0.5, color=c, lw=0, zorder=0)


def tick_bands(ax, edges, horizontal=False):
    """alternating gray / white bands between consecutive tick positions of a continuous axis"""
    for i in range(len(edges) - 1):
        if i % 2 == 0:
            (ax.axhspan if horizontal else ax.axvspan)(edges[i], edges[i + 1], color=BAND, lw=0, zorder=0)


def frame(ax):
    for s in ("left", "bottom"):
        ax.spines[s].set_color(MUTED)
    ax.set_axisbelow(True)


def save(fig, name):
    fig.savefig(FIG / f"{name}.pdf", bbox_inches="tight", pad_inches=0.02)
    fig.savefig(FIG / f"{name}.png", bbox_inches="tight", pad_inches=0.02, dpi=220)
    plt.close(fig)


def load_eval(run):
    out = []
    for p in sorted((ROOT / "results" / "eval" / run).glob("*.json")):
        if p.stem.isdigit():
            out.append(json.load(open(p, encoding="utf-8")))
    return out


# ================================================================================ Figure 1: overview (3 panels)
PAGE = {"/": ("home", "#D3D7DC"), "/customer/account/": ("acct.", BLUE_T), "/sales/order/history/": ("orders", TEAL_T),
        "/checkout/cart/": ("cart", CORAL_T), "/customer/account/login/": ("login", GOLD_T),
        "/wishlist/": ("wish", PLUM_T), "/sales/order/view/order_id/158/": ("order", TEAL_T)}


def teaser_trace(ax):
    """(a) a real ReAct episode on task 440: what the agent clicks, which page it sees, which request leaves the
    browser, and how many server changes have piled up. Every label sits fully inside one cell or fully above the
    grid, so no text crosses a cell border."""
    e = json.load(open(ROOT / "results" / "eval" / "react" / "440.json", encoding="utf-8"))
    steps = [st["executed"][0] for st in e["steps"]][:12]
    n = len(steps)
    top = 3.75
    ax.set_xlim(-2.9, n - 0.4)
    ax.set_ylim(-0.4, 6.55)
    ax.axis("off")
    for i in range(n):
        ax.add_patch(Rectangle((i - 0.5, 0.0), 1, top, fc=BAND if i % 2 == 0 else WHITE, lw=0, zorder=0))
    lanes = [(3.32, "element"), (2.45, "page shown\n(client state)"), (1.55, "request\nmethod"),
             (0.6, "server changes\nso far (DB)"), (top + 0.55, "agent clicks")]
    for y, lab in lanes:
        ax.text(-0.68, y, lab, ha="right", va="center", fontsize=5.8, color=INK2, linespacing=1.1)
    for y in (2.95, 2.0, 1.12):
        ax.plot([-0.5, n - 0.5], [y, y], color=RULE, lw=0.5, ls=(0, (1, 1.5)), zorder=1)
    ax.plot([-0.5, n - 0.5], [0, 0], color=MUTED, lw=0.6, zorder=1)
    cum = 0
    for i, ex in enumerate(steps):
        el = ex.get("element") or {}
        name = (el.get("name") or "").strip().replace("*", "")
        name = {"My Wish List 1 item": "Wish List"}.get(name, name)
        hit = ex["state_changed"]
        # element type inside its cell (a = link), the control's name rotated above the grid
        tag = {"link": "link", "textbox": "field"}.get(el.get("role", ""), el.get("role", ""))
        ax.text(i, 3.32, tag, ha="center", va="center", fontsize=3.6, color=CORAL if hit else MUTED, zorder=3)
        ax.text(i - 0.05, top + 0.12, name, rotation=55, ha="left", va="bottom", fontsize=4.8,
                rotation_mode="anchor", color=CORAL if hit else INK, weight="bold" if hit else "normal", zorder=3)
        # page chip
        path = ex["url"].replace("http://localhost:7770", "")
        lab, col = PAGE.get(path, (path, "#DDDDDD"))
        box(ax, i - 0.45, 2.17, 0.9, 0.56, col, "white", lw=0.6, r=0.1, z=2)
        ax.text(i, 2.45, lab, ha="center", va="center", fontsize=3.7, color=INK, zorder=3)
        # request method
        if ex.get("net_mutation"):
            ax.add_patch(Rectangle((i - 0.42, 1.34), 0.84, 0.42, fc=CORAL if hit else "white", ec=CORAL, lw=0.7,
                                   zorder=3))
            ax.text(i, 1.55, "POST", ha="center", va="center", fontsize=3.4, color="white" if hit else CORAL,
                    weight="bold", zorder=4)
        else:
            ax.text(i, 1.55, "GET", ha="center", va="center", fontsize=3.8, color=MUTED, zorder=4)
        # server changes so far: one block per change, the new one filled
        cum += hit
        for k in range(cum):
            new = hit and k == cum - 1
            ax.add_patch(Rectangle((i - 0.3, 0.1 + k * 0.235), 0.6, 0.19, fc=CORAL if new else CORAL_T,
                                   ec=CORAL, lw=0.6 if new else 0.3, zorder=3))
        if hit:
            ax.add_patch(Rectangle((i - 0.5, 0.0), 1, top, fc="none", ec=CORAL, lw=0.9, zorder=5))
        ax.text(i, -0.12, str(i), ha="center", va="top", fontsize=5, color=MUTED)
    ax.text(-0.68, -0.12, "step", ha="right", va="top", fontsize=5.4, color=MUTED)
    # the only note inside the grid sits in empty cells of the DB lane (steps 3-6), clear of every border
    ax.text(4.5, 0.68, "Sign Out is a POST\nthat changes no data", fontsize=4.1, color=INK2, ha="center",
            va="center", linespacing=1.0, zorder=6)
    ax.text(-2.9, 6.5, "(a) A real episode: ReAct on WebArena task 440", fontsize=7, weight="bold", va="top")
    ax.text(-2.9, 6.1, "“reorder the make-up kit I cancelled”: three clicks on a link named Reorder add\n"
            "three server changes; go_back would restore the page, not the rows. Task failed.",
            fontsize=5.3, color=INK2, va="top", style="italic", linespacing=1.1)


def teaser_map(ax):
    """(b) every RACE step in the (u, rho) plane with the three routing regions"""
    rng = np.random.RandomState(3)
    pts = []
    for e in load_eval("race"):
        for st in e["steps"]:
            r = st.get("rho_top")
            if r is None:
                continue
            ch = any(ex["state_changed"] and not ex.get("rollback") for ex in st["executed"])
            pts.append((st["u"], max(r, 1.6e-3), ch))
    u = np.array([p[0] for p in pts])
    r = np.array([p[1] for p in pts])
    c = np.array([p[2] for p in pts])
    uj = u + rng.uniform(-0.12, 0.12, len(u))
    lo = 1.6e-4
    ax.set_xlim(-0.2, 1.2)
    ax.set_ylim(lo, 9.0)
    ax.set_yscale("log")
    ax.add_patch(Rectangle((-0.2, lo), 0.7, 0.5 - lo, fc=BAND, lw=0, zorder=0))
    ax.add_patch(Rectangle((0.5, lo), 0.7, 0.5 - lo, fc=TEAL_T, lw=0, zorder=0))
    ax.add_patch(Rectangle((-0.2, 0.5), 1.4, 8.5, fc=PLUM_T, lw=0, zorder=0))
    ax.axhline(0.5, color=PLUM, lw=0.7, ls="--", zorder=1)
    ax.plot([0.5, 0.5], [lo, 0.5], color=TEAL, lw=0.7, ls="--", zorder=1)
    ax.scatter(uj[~c], r[~c], s=2.5, color="#7D8794", alpha=0.35, lw=0, zorder=2)
    ax.scatter(uj[c], r[c], s=26, marker="*", color=CORAL, edgecolor="white", lw=0.4, zorder=4)
    n2 = int((c & (r > 0.5)).sum())
    n1 = int((c & (u > 0.5) & (r <= 0.5)).sum())
    n0 = int(c.sum()) - n1 - n2
    lab = dict(ha="center", va="center", zorder=5, linespacing=1.05)
    ax.text(0.5, 2.6, f"M2 imagine & verify\n{n2} changes", color=PLUM, fontsize=4.5, weight="bold", **lab)
    ax.text(0.15, 4.5e-4, f"M0 execute\n{n0} changes", color=INK2, fontsize=4.7, weight="bold", **lab)
    ax.text(0.85, 4.5e-4, f"M1 try & back\n{n1} changes", color=CORAL, fontsize=4.7, weight="bold", **lab)
    ax.set_xticks([0, 0.58, 1])
    ax.set_xticklabels(["0\nagree", "0.58\n2 vs 1", "1\nall differ"], fontsize=5.3)
    ax.set_yticks([1e-2, 1e-1, 0.5, 1])
    ax.set_yticklabels(["0.01", "0.1", "0.5", "1"], fontsize=5.5)
    ax.set_xlabel("vote uncertainty $u$", fontsize=6.0, labelpad=1)
    ax.set_ylabel(r"predicted irreversibility $\rho$", fontsize=6.0, labelpad=1)
    frame(ax)
    ax.set_title("(b) RACE routing", loc="left", fontsize=7, pad=12)
    ax.legend(handles=[Line2D([], [], marker="o", ls="", color="#7D8794", ms=2.5, label="step"),
                       Line2D([], [], marker="*", ls="", color=CORAL, ms=6, label="changed server")],
              loc="lower left", bbox_to_anchor=(-0.04, 0.995), ncol=2, frameon=False, fontsize=5.0,
              handletextpad=0.2, columnspacing=0.6, borderaxespad=0)


def badge(ax, x, y, k, col, size=30):
    """a numbered round badge drawn as a marker (round at any aspect ratio), never clipped"""
    ax.scatter([x], [y], s=size, color=col, edgecolor="white", linewidth=0.6, zorder=8, clip_on=False)
    ax.text(x, y, str(k), fontsize=4.6, color="white", weight="bold", ha="center", va="center", zorder=9,
            clip_on=False)


def teaser_checks(ax):
    """(c) where a check can sit, with the share of side effects each check point catches"""
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.text(0.0, 1.0, "(c) Where can a check stop it?", fontsize=7, weight="bold", va="top")
    nodes = [(0.125, "brain", "world\nmodel", PLUM), (0.38, "web", "browser\npage", BLUE),
             (0.635, "shield", "request\ngate", CORAL), (0.89, "database", "server\ndatabase", None)]
    yy, hw, hh = 0.73, 0.1, 0.12
    for k, (x, ic, lab, col) in enumerate(nodes, 1):
        box(ax, x - hw, yy - hh, 2 * hw, 2 * hh, "white", RULE if col is None else col, lw=0.8, r=0.025, z=2)
        icon(ax, ic, (x, yy + 0.045), zoom=0.1)
        ax.text(x, yy - 0.065, lab, fontsize=4.2, ha="center", va="center", color=INK2, linespacing=0.95, zorder=7)
        if col is not None:   # badge above the box, outside its border
            badge(ax, x, yy + hh + 0.045, k, col)
    for (x0, *_), (x1, *_) in zip(nodes[:-1], nodes[1:]):
        arrow(ax, (x0 + hw + 0.006, yy), (x1 - hw - 0.006, yy), color=INK2, lw=0.8, ms=5)
    ax.text(0.0, 0.535, "side effects caught before they reach the server", fontsize=4.9, color=INK2)
    rows = [(1, "world model announces the change", 34 / 45, "34 / 45", PLUM),
            (2, "page predictor, learned", S["pred_points"]["learned"]["recall"], None, BLUE),
            (2, "page predictor, keyword rule", S["pred_points"]["heuristic"]["recall"], None, BLUE),
            (3, "hold every non-GET request", S["pred_points"]["http_gate"]["recall"], None, CORAL)]
    x0, w, h, step = 0.095, 0.69, 0.048, 0.118
    for k, (num, lab, v, txt, col) in enumerate(rows):
        y = 0.40 - k * step
        # band spans the label and the bar of this row with margin on both sides
        ax.add_patch(Rectangle((0.0, y - 0.02), 1.0, step, fc=BAND if k % 2 == 0 else WHITE, lw=0, zorder=0))
        badge(ax, 0.042, y + h / 2, num, col, size=26)
        ax.text(x0, y + h + 0.008, lab, fontsize=4.6, va="bottom", ha="left", color=INK, zorder=4)
        ax.add_patch(Rectangle((x0, y), w, h, fc="white", ec=RULE, lw=0.5, zorder=2))
        ax.add_patch(Rectangle((x0, y), w * v, h, fc=col, lw=0, zorder=3))
        ax.text(x0 + w + 0.02, y + h / 2, txt or f"{100 * v:.1f}%", fontsize=5.5, va="center", ha="left",
                color=col, weight="bold")
    ax.text(0.0, -0.035, "check 1 sees most commits coming; the agent runs them anyway", fontsize=4.5,
            color=MUTED, style="italic", va="bottom")


def check_collisions(fig):
    """report text that crosses a border: a text box that partly overlaps a drawn edge (patch with an edge or a
    line), or two text boxes that overlap. Background fills without edges are ignored."""
    from matplotlib.text import Text
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    texts = [t for t in fig.findobj(Text) if t.get_visible() and t.get_text().strip()]
    boxes = [(t, t.get_window_extent(rend).expanded(1.0, 1.0)) for t in texts]
    issues = []
    for ax in fig.axes:
        for p in ax.patches:
            ec = p.get_edgecolor()
            if p.get_linewidth() == 0 or ec[3] == 0:
                continue
            pb = p.get_window_extent(rend)
            for t, tb in boxes:
                if t.axes is not ax and t.figure is not fig:
                    continue
                inter = tb.overlaps(pb)
                inside = pb.x0 <= tb.x0 and tb.x1 <= pb.x1 and pb.y0 <= tb.y0 and tb.y1 <= pb.y1
                around = tb.x0 <= pb.x0 and pb.x1 <= tb.x1 and tb.y0 <= pb.y0 and pb.y1 <= tb.y1
                if inter and not inside and not around and tb.width > 1 and pb.width > 2:
                    issues.append(f"text {t.get_text()[:28]!r} crosses edge of {type(p).__name__}")
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i][1], boxes[j][1]
            if boxes[i][0].get_rotation() and boxes[j][0].get_rotation():
                continue  # parallel rotated labels: their axis-aligned boxes overlap, the glyphs do not
            if a.overlaps(b) and a.width > 1 and b.width > 1:
                issues.append(f"text {boxes[i][0].get_text()[:20]!r} overlaps {boxes[j][0].get_text()[:20]!r}")
    return issues


def fig_teaser():
    fig = plt.figure(figsize=(6.15, 2.5))
    teaser_trace(fig.add_axes([0.0, 0.0, 0.47, 1.0]))
    teaser_map(fig.add_axes([0.545, 0.16, 0.155, 0.66]))
    teaser_checks(fig.add_axes([0.735, 0.04, 0.265, 0.9]))
    for msg in check_collisions(fig):
        print("[teaser]", msg)
    save(fig, "teaser")


# ================================================================================ Figure 2: where side effects happen
def fig_where():
    fig, axs = plt.subplots(1, 2, figsize=(7.0, 1.8), gridspec_kw={"width_ratios": [1, 1.1], "wspace": 0.62})
    ax = axs[0]
    order = ["click: mutating label", "click: mutating label (link)", "key press", "click: other label",
             "fill / select", "navigation / answer"]
    cues = S["by_cue"]
    order = [k for k in order if k in cues]
    vals = [100 * cues[k]["p_change"] for k in order]
    yy = np.arange(len(order))[::-1]
    row_bands(ax, len(order))
    cols = [CORAL if "link" not in k else GOLD for k in order]
    cols = [c if v > 1 else BLUE for c, v in zip(cols, vals)]
    ax.barh(yy, [max(v, 0.25) for v in vals], color=cols, height=0.56, zorder=2)
    for y, v, k in zip(yy, vals, order):
        ax.text(max(v, 0.25) + 0.8, y, f"{v:.1f}%   n={cues[k]['n']:,} ({100 * cues[k]['share']:.0f}%)",
                va="center", fontsize=5.8, color=INK2)
    ax.set_yticks(yy)
    ax.set_yticklabels(order)
    ax.set_ylim(-0.5, len(order) - 0.5)
    ax.set_xlabel("P(action changes server state), %")
    ax.set_xlim(0, 80)
    ax.tick_params(axis="y", length=0)
    frame(ax)
    ax.set_title(f"(a) {S['n_actions']:,} executed actions of 8 agents", loc="left")

    ax = axs[1]
    items = S["culprit_names"][:12]
    counts = [c for n, r, c in items]
    rolec = [GOLD if r == "link" else BLUE for n, r, c in items]
    yy = np.arange(len(items))[::-1]
    row_bands(ax, len(items))
    ax.barh(yy, counts, color=rolec, height=0.6, zorder=2)
    for y, c in zip(yy, counts):
        ax.text(c + 0.4, y, str(c), va="center", fontsize=5.8, color=INK2)
    ax.set_yticks(yy)
    ax.set_yticklabels([n for n, r, c in items], fontsize=6)
    ax.set_ylim(-0.5, len(items) - 0.5)
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel("server-state changes")
    ax.set_xlim(0, 32)
    frame(ax)
    ax.set_title(f"(b) controls behind the {S['n_changes']} changes", loc="left")
    ax.legend(handles=[Patch(color=BLUE, label="button"), Patch(color=GOLD, label="link")], frameon=True,
              facecolor="white", edgecolor="none", framealpha=0.95, loc="lower right", handlelength=1,
              handleheight=0.8)
    save(fig, "where")


# ================================================================================ Figure 3: predictability
def fig_predict():
    fig, ax = plt.subplots(figsize=(3.3, 1.95))
    tick_bands(ax, [0.4, 0.5, 1, 2, 5, 10, 20, 50, 100])
    c = np.array(S["pred_curves"]["learned"])
    ax.plot(100 * c[:, 0], 100 * c[:, 1], color=BLUE, lw=1.5, label="learned predictor (page features)", zorder=3)
    h = np.array(S["pred_curves"]["heuristic"])
    h = h[h[:, 0] > 0]
    ax.plot(100 * h[:, 0], 100 * h[:, 1], color=TEAL, lw=0.9, ls=(0, (2, 1.5)), marker="o", ms=3.6, zorder=3,
            label="keyword rule (4 thresholds)", markeredgecolor="white", markeredgewidth=0.5)
    g = S["pred_points"]["http_gate"]
    ax.scatter([100 * g["flag_share"]], [100 * g["recall"]], marker="*", s=120, color=CORAL, zorder=6,
               edgecolor="white", linewidth=0.6, label="HTTP gate (hold non-GET requests)")
    for nm, col in (("learned", BLUE), ("heuristic", TEAL)):
        p = S["pred_points"][nm]
        ax.scatter([100 * p["flag_share"]], [100 * p["recall"]], s=24, color=col, edgecolor=INK, linewidth=0.6,
                   zorder=7)
    ax.annotate(r"$\tau_\rho=0.5$", (100 * S["pred_points"]["learned"]["flag_share"],
                                     100 * S["pred_points"]["learned"]["recall"]), xytext=(12, -11),
                textcoords="offset points", fontsize=5.8, color=INK2,
                arrowprops=dict(arrowstyle="-", lw=0.5, color=MUTED))
    ax.annotate(f"{100 * g['recall']:.1f}% caught,\n{100 * g['flag_share']:.1f}% of actions held",
                (100 * g["flag_share"], 100 * g["recall"]), xytext=(-66, -20), textcoords="offset points",
                fontsize=5.8, color=CORAL, arrowprops=dict(arrowstyle="-", lw=0.5, color=CORAL))
    ax.set_xscale("log")
    ax.set_xlim(0.4, 100)
    ax.set_ylim(0, 105)
    ax.set_xticks([0.5, 1, 2, 5, 10, 20, 50, 100])
    ax.set_xticklabels(["0.5", "1", "2", "5", "10", "20", "50", "100"])
    ax.set_xlabel("actions held for checking, % (log)")
    ax.set_ylabel("side effects caught, %")
    frame(ax)
    ax.legend(frameon=True, facecolor="white", edgecolor="none", framealpha=0.95, loc="lower right", fontsize=5.9)
    save(fig, "predict")


# ================================================================================ Figure 4: outcomes
def fig_outcomes():
    fig, axs = plt.subplots(1, 3, figsize=(7.0, 1.95), sharey=True,
                            gridspec_kw={"width_ratios": [1, 1, 1.15], "wspace": 0.1})
    M = S["methods"]
    base = M["react"]["tokens"]
    yy = np.arange(len(ORDER))[::-1]
    for ax, key, lab in ((axs[0], "SR", "success rate, % (95% CI)"),
                         (axs[1], "IER", "irreversible-error rate, % (95% CI)")):
        row_bands(ax, len(ORDER))
        ax.axvline(100 * M["react"][key], color=MUTED, lw=0.6, ls="--", zorder=1)
        for y, m in zip(yy, ORDER):
            v = M[m]
            lo, hi = 100 * v[key + "_ci"][0], 100 * v[key + "_ci"][1]
            col = CORAL if m == "race" else BLUE
            ax.plot([lo, hi], [y, y], color=col, lw=2.2, alpha=0.3, solid_capstyle="round", zorder=2)
            ax.scatter([100 * v[key]], [y], s=24, color=col, edgecolor="white", linewidth=0.7, zorder=4)
            ax.text(hi + 0.4, y, f"{100 * v[key]:.1f}", va="center", fontsize=5.6, color=INK2)
        ax.set_xlabel(lab)
        frame(ax)
    axs[0].set_xlim(10, 42)
    axs[1].set_xlim(0, 15.5)
    axs[0].set_ylim(-0.5, len(ORDER) - 0.5)
    axs[0].set_yticks(yy)
    axs[0].set_yticklabels([f"{NAMES[m]} ({M[m]['tokens'] / base:.2f}x)" for m in ORDER])
    axs[0].get_yticklabels()[-1].set_color(CORAL)
    axs[0].set_title("(a) task success", loc="left")
    axs[1].set_title("(b) irreversible errors", loc="left")

    ax = axs[2]
    row_bands(ax, len(ORDER))
    A = S["ier_anatomy"]
    cats = [("wrong_commit", "one wrong commit", BLUE), ("repeated", "repeated commit", GOLD),
            ("collateral", "read-only task", PLUM)]
    left = np.zeros(len(ORDER))
    for k, lab, col in cats:
        v = np.array([A[m].get(k, 0) for m in ORDER], float)
        ax.barh(yy, v, left=left, color=col, height=0.6, label=lab, edgecolor="white", linewidth=0.9, zorder=2)
        left += v
    for y, t in zip(yy, left):
        ax.text(t + 0.25, y, f"{int(t)}", va="center", fontsize=5.6, color=INK2)
    ax.set_xlabel(f"tasks with an irreversible error (of {S['n_tasks']})")
    ax.set_title("(c) anatomy of those errors", loc="left")
    ax.legend(frameon=True, facecolor="white", edgecolor="none", framealpha=0.95, fontsize=5.6, loc="lower right",
              handlelength=0.9, handleheight=0.7, labelspacing=0.3)
    ax.set_xlim(0, 19)
    frame(ax)
    for ax in axs:
        ax.tick_params(axis="y", length=0)
    save(fig, "outcomes")


# ================================================================================ Figure 5: power
def fig_power():
    fig, ax = plt.subplots(figsize=(3.3, 1.8))
    tick_bands(ax, [1.5, 2, 4, 6, 8, 10, 12, 14, 15])
    for (d, pts), col in zip(S["power_curve"].items(), SEQ):
        p = np.array(pts)
        ax.plot(100 * p[:, 0], p[:, 1], color=col, lw=1.5, label=f"{100 * float(d):.0f}% of tasks discordant",
                zorder=3)
    for n, lab, va in ((134, "our test set (134)", "top"), (165, "WebArena-Lite (165)", "bottom"),
                       (812, "WebArena (812)", "bottom")):
        ax.axhline(n, color=INK2, lw=0.6, ls=(0, (3, 2)), zorder=2)
        ax.text(5.2, n * (0.92 if va == "top" else 1.08), lab, fontsize=5.6, color=INK2, ha="left", va=va,
                bbox=dict(fc="white", ec="none", pad=0.4, alpha=0.9), zorder=4)
    ax.axvspan(1.5, 3.7, color=CORAL_T, alpha=0.9, lw=0, zorder=1)
    ax.text(2.6, 38, "|ΔSR| observed\nbetween methods", fontsize=5.3, color=CORAL, ha="center", va="bottom",
            zorder=4)
    ax.set_yscale("log")
    ax.set_xlim(1.5, 15)
    ax.set_ylim(30, 5000)
    ax.set_xlabel("true difference in success rate, points")
    ax.set_ylabel("paired tasks needed\n(80% power, α=0.05)")
    frame(ax)
    ax.legend(frameon=True, facecolor="white", edgecolor="none", framealpha=0.95, fontsize=5.6, loc="upper right")
    save(fig, "power")


if __name__ == "__main__":
    fig_teaser()
    fig_where()
    fig_predict()
    fig_outcomes()
    fig_power()
    print("written to", FIG)
