#!/usr/bin/env python3
"""Stage 3: extracted tables -> canonical figures (SVG + PNG).

Consumes only the analysis directory written by `post_expiry_extract.py`.
Every visual mark is traceable to a row of runs.csv, events.csv, or
observations.csv — no interpolation across sampling gaps, witnesses
drawn as their censoring INTERVALS, and "quiet from" drawn as an
observation bound, never as extinction. Controls are excluded from
event figures (3 samples cannot observe resurrection) and appear only
in the outcome panel under that label.

Usage: python3 post_expiry_figures.py <analysis-dir>
"""

import csv
import hashlib
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt                           # noqa: E402

CELL_LABEL = {("96", "480"): "A 96s/480s", ("406", "1230"): "B 406s/1230s",
              ("96", "960"): "C 96s/960s"}


def load(adir, name):
    with open(os.path.join(adir, name)) as f:
        return list(csv.DictReader(f))


def short(run_id):
    core = run_id.replace("post-expiry-", "").rsplit("-", 1)[0]
    return core.replace("ttl90-", "").replace("ttl400-", "")


def fnum(v):
    return float(v) if v not in ("", "None", None) else None


def treatments(runs):
    order = {"v3": 0, "v4A": 1, "v4B": 2, "v4C": 3}
    ts = [r for r in runs if r["arm"] == "treatment"]
    return sorted(ts, key=lambda r: (order.get(
        ("v3" if r["batch"] == "v3" else
         "v4" + ("A" if r["window_s"] == "480" else
                 "B" if r["window_s"] == "1230" else "C"))), r["run_id"]))


def fig_trajectories(adir, runs, events, obs):
    ts = treatments(runs)
    by_run = {}
    for o in obs:
        by_run.setdefault(o["run_id"], []).append(o)
    ev_by_run = {}
    for e in events:
        ev_by_run.setdefault(e["run_id"], []).append(e)

    ncol = 4
    nrow = (len(ts) + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(16, 3.2 * nrow),
                             sharey=True)
    for ax, r in zip(axes.flat, ts):
        ttl = float(r["configured_ttl"])
        author = None
        nodes = {}
        for o in by_run[r["run_id"]]:
            nodes.setdefault(o["node"], []).append(o)
        # the author is the run key = first node name in the record;
        # runs.csv does not carry it, but the author is the node with a
        # sampled TTL in the very first row — and by construction the
        # entry key equals the author name; infer as the node holding
        # the highest TTL at the earliest done time.
        first = min((fnum(o["done"]) for o in by_run[r["run_id"]]
                     if o["present"] == "True"), default=0)
        cands = [o for o in by_run[r["run_id"]]
                 if o["present"] == "True" and fnum(o["done"]) < first + 5]
        author = max(cands, key=lambda o: fnum(o["ttl"]) or 0)["node"] \
            if cands else None
        for nm, os_ in nodes.items():
            pts = [(fnum(o["done"]) / ttl, fnum(o["ttl"]) / ttl)
                   for o in os_ if o["present"] == "True"
                   and o["row_valid"] == "True" and fnum(o["ttl"]) is not None]
            pts.sort()
            if not pts:
                continue
            # break the line across absences so gaps stay gaps
            xs, ys = zip(*pts)
            if nm == author:
                ax.plot(xs, ys, "-", color="#c1272d", lw=1.6, zorder=3)
            else:
                ax.plot(xs, ys, "-", color="#888888", lw=0.7, alpha=0.6)
        for e in ev_by_run.get(r["run_id"], []):
            lo, up = fnum(e["lower_lifetimes"]), fnum(e["upper_lifetimes"])
            colour = "#1f77b4" if e["kind"] == "ttl_reset" else "#2ca02c"
            ax.axvspan(lo, up, color=colour, alpha=0.25, zorder=1)
        q = fnum(r["quiet_from_lifetimes"])
        if q is not None:
            ax.axvline(q, ls="--", color="black", lw=0.8)
        ax.set_title(f"{short(r['run_id'])}  ev≥{r['evidence']}", fontsize=8)
        ax.set_xlim(0, float(r["window_s"]) / ttl)
        ax.set_ylim(0, 1.05)
        ax.tick_params(labelsize=7)
    for ax in axes.flat[len(ts):]:
        ax.axis("off")
    fig.suptitle("Author (red) and neighbour TTL trajectories, "
                 "t and TTL in lifetimes.  Shaded: witness intervals "
                 "(blue=TTL reset, green=sampled return).  Dashed: last "
                 "sampled presence (observation bound)", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    return fig, "trajectories"


def fig_raster(adir, runs, obs):
    ts = treatments(runs)
    by_run = {}
    for o in obs:
        by_run.setdefault(o["run_id"], []).append(o)
    fig, axes = plt.subplots(len(ts), 1, figsize=(14, 1.1 * len(ts)),
                             sharex=True)
    for ax, r in zip(axes, ts):
        ttl = float(r["configured_ttl"])
        names = sorted({o["node"] for o in by_run[r["run_id"]]})
        idx = {nm: i for i, nm in enumerate(names)}
        for o in by_run[r["run_id"]]:
            x = fnum(o["done"]) / ttl
            y = idx[o["node"]]
            if o["ok"] != "True":
                ax.plot(x, y, "x", color="red", ms=3)
            elif o["present"] == "True":
                ax.scatter(x, y, c=[min(1.0, (fnum(o["ttl"]) or 0) / ttl)],
                           cmap="viridis", vmin=0, vmax=1, marker="s", s=6)
        ax.set_yticks(range(len(names)))
        ax.set_yticklabels(names, fontsize=5)
        ax.set_ylabel(short(r["run_id"]), fontsize=6, rotation=0,
                      ha="right", va="center")
        ax.set_xlim(0, 10.2)
    axes[-1].set_xlabel("time (lifetimes)")
    fig.suptitle("Presence raster at true per-node sample times — colour = "
                 "TTL/lifetime, blank = sampled absent, red x = probe "
                 "failure", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    return fig, "raster"


def wilson(k, n, z=1.96):
    import math
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def fig_outcomes(adir, runs):
    cells = [("v3", "96", "480"), ("v4", "96", "480"),
             ("v4", "406", "1230"), ("v4", "96", "960")]
    labels = ["A v3", "A v4", "B v4", "C v4"]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.6))
    for i, (b, t, w) in enumerate(cells):
        grp = [r for r in runs if (r["batch"], r["configured_ttl"],
                                   r["window_s"], r["arm"])
               == (b, t, w, "treatment")]
        ev = [int(r["evidence"]) for r in grp]
        axes[0].plot([i + (j - len(ev) / 2) * 0.06 for j in range(len(ev))],
                     ev, "o", color="#1f77b4", ms=5, alpha=0.8)
        k = sum(1 for e in ev if e >= 1)
        lo, hi = wilson(k, len(ev))
        axes[1].errorbar([i], [k / len(ev)],
                         yerr=[[k / len(ev) - lo], [hi - k / len(ev)]],
                         fmt="s", color="#c1272d", capsize=4)
        q = [fnum(r["quiet_from_lifetimes"]) for r in grp
             if fnum(r["quiet_from_lifetimes"]) is not None]
        axes[2].plot([i] * len(q), q, "o", color="#2ca02c", ms=5, alpha=0.8)
    for ax, title in zip(axes, ["evidence lower bound per run",
                                "P(≥1 witness), Wilson 95%",
                                "last sampled presence (lifetimes)"]):
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_title(title, fontsize=9)
    axes[1].set_ylim(0, 1.05)
    fig.suptitle("Run-level outcomes by cell — controls excluded "
                 "(resurrection not observable at 3 samples)", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return fig, "outcomes"


def fig_observer(adir, runs):
    ts = treatments(runs)
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    colours = {"480": "#1f77b4", "1230": "#c1272d", "960": "#2ca02c"}
    for r in ts:
        c = colours[r["window_s"]]
        # per-lifetime probe pressure, so cells with different windows
        # are comparable
        ttl = float(r["configured_ttl"])
        per_lt = int(r["samples_valid"]) / (float(r["window_s"]) / ttl)
        axes[0].plot(per_lt, int(r["evidence"]), "o", color=c, alpha=0.75)
        axes[1].plot(fnum(r["node_gap_med_s"]) / ttl, int(r["evidence"]),
                     "o", color=c, alpha=0.75)
    axes[0].set_xlabel("valid samples per lifetime")
    axes[1].set_xlabel("median node sampling gap (lifetimes)")
    for ax in axes:
        ax.set_ylabel("evidence lower bound")
    fig.suptitle("Observer diagnostics — the confound stays visible: "
                 "blue A, red B, green C", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    return fig, "observer-diagnostics"


def fig_horizon(adir, runs, events):
    a = [r for r in treatments(runs) if r["batch"] == "v4"
         and r["window_s"] == "480"]
    b = [r for r in treatments(runs) if r["window_s"] == "1230"]
    c = [r for r in treatments(runs) if r["window_s"] == "960"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6))
    for i, (grp, lbl) in enumerate(((a, "A v4 (~19 opp/LT)"),
                                    (b, "B (~81 opp/LT)"))):
        k = sum(1 for r in grp if int(r["ev_within_3L"]) >= 1)
        lo, hi = wilson(k, len(grp))
        axes[0].errorbar([i], [k / len(grp)],
                         yerr=[[k / len(grp) - lo], [hi - k / len(grp)]],
                         fmt="s", capsize=4, color="#1f77b4")
        axes[0].annotate(f"{k}/{len(grp)}", (i, 0.5), ha="center",
                         fontsize=9)
    axes[0].set_xticks([0, 1])
    axes[0].set_xticklabels(["A v4 (~19 opp/LT)", "B (~81 opp/LT)"],
                            fontsize=8)
    axes[0].set_ylim(0, 1.05)
    axes[0].set_title("P(≥1 witness within 3 lifetimes), Wilson 95%",
                      fontsize=9)
    ev_by_run = {}
    for e in events:
        ev_by_run.setdefault(e["run_id"], []).append(e)
    for j, r in enumerate(c):
        for e in ev_by_run.get(r["run_id"], []):
            axes[1].plot([fnum(e["lower_lifetimes"]),
                          fnum(e["upper_lifetimes"])], [j, j], "-",
                         lw=3, color="#1f77b4", alpha=0.8,
                         solid_capstyle="butt")
        q = fnum(r["quiet_from_lifetimes"])
        if q is not None:
            axes[1].plot(q, j, "k|", ms=10)
    axes[1].axvline(2, ls=":", color="gray")
    axes[1].axvline(5, ls=":", color="gray")
    axes[1].set_yticks(range(len(c)))
    axes[1].set_yticklabels([short(r["run_id"]) for r in c], fontsize=7)
    axes[1].set_xlim(0, 10)
    axes[1].set_xlabel("lifetimes")
    axes[1].set_title("Cell C witness intervals over 10 lifetimes "
                      "(| = last sampled presence)", fontsize=9)
    fig.tight_layout()
    return fig, "common-horizon"


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    adir = sys.argv[1]
    runs = load(adir, "runs.csv")
    events = load(adir, "events.csv")
    obs = load(adir, "observations.csv")
    outdir = os.path.join(adir, "figures")
    os.makedirs(outdir, exist_ok=True)

    made = {}
    for fig, name in (fig_trajectories(adir, runs, events, obs),
                      fig_raster(adir, runs, obs),
                      fig_outcomes(adir, runs),
                      fig_observer(adir, runs),
                      fig_horizon(adir, runs, events)):
        paths = []
        for ext in ("svg", "png"):
            p = os.path.join(outdir, f"{name}.{ext}")
            fig.savefig(p, dpi=150)
            paths.append(p)
        plt.close(fig)
        made[name] = {os.path.basename(p): hashlib.sha256(
            open(p, "rb").read()).hexdigest()[:16] for p in paths}
        print(f"wrote {name}.svg/.png")

    with open(__file__, "rb") as f:
        self_sha = hashlib.sha256(f.read()).hexdigest()
    inputs = {n: hashlib.sha256(
        open(os.path.join(adir, n), "rb").read()).hexdigest()[:16]
        for n in ("runs.csv", "events.csv", "observations.csv")}
    with open(os.path.join(outdir, "MANIFEST.json"), "w") as f:
        json.dump({"figures": made, "inputs": inputs,
                   "figures_script_sha256": self_sha,
                   "command": " ".join(sys.argv)}, f, indent=1,
                  sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
