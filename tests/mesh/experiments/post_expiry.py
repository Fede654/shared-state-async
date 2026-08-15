#!/usr/bin/env python3
"""What happens AFTER the author's entry expires? (the T24 follow-up)

THE QUESTION THIS ANSWERS

`divergence_dynamics.py` measured TTL divergence as a rate over a fixed
five-minute window. The write-up then argued the effect was self-limiting
because the author's copy bleaches away at ~2431 s and "nothing refreshes
it". External review found that wrong in the code: `sharedstate.cc:866`
inserts a MISSING key before the own-authorship guard at `:882` can see
it, so an expired author adopts a neighbour's echo of its own entry
instead of staying empty. **T24** reproduces that deterministically.

T24 proves the mechanism: the missing-key path accepts an own-authored
remote entry. It cannot show whether that happens without injection or
recurs, because it injects the echo itself. That is what this measures.

So this runs the whole cycle PEER-GENERATED AND NOT INJECTED — real
nodes, real inflation from real transfer duration, one publish and
never again, watched for many entry lifetimes. That is the full claim:
it does NOT mean "in an ordinary, unobserved mesh", since the dense
observer load is itself a factor (see below).

WHY A SHORT TTL, AND WHAT THAT COSTS

The field TTL is 2400 s, so one cycle would take 40 minutes and a
multi-cycle run most of a day. This uses a short bleach TTL (default 90 s,
giving a 96 s insert TTL) so a cycle takes a couple of minutes.

That trade is NOT free, and it is worse than "the rate will differ". The
short cell runs interval/TTL = 5/96, ~19 gossip opportunities per
lifetime; production runs 30/2400, which is 80 — this cell has about
4.2 times FEWER opportunities per lifetime than production, and the
number of opportunities per lifetime is plausibly the very thing that
decides whether the loop sustains. An earlier default of 20 s was worse
still: the entry reached all five nodes at t~28 s with the author
already at TTL 3 of 24, so propagation took about as long as the entry
lived, which makes a question about what happens AFTER expiry
unanswerable. **Nothing measured here transfers to production**, not the
rate and not the yes/no. An earlier version of this docstring claimed
the qualitative answer transfers, on the strength of a `spread/TTL`
ratio comparison; that comparison used uncorrected spread against a
"field range" which is itself the unsupported five-minute extrapolation.
Withdrawn. Treat this as a short-TTL model whose only role is to say
what happens in a short-TTL model.

WHY THE OBSERVER IS A PROBLEM HERE — AND WHY THERE IS A CONTROL

An earlier version argued the observer was harmless: `Node.probe()`
sends an EMPTY slice and `merge` only inserts keys present in the
incoming slice (`sharedstate.cc:864`), so a probe cannot *cause* a
resurrection. True, and beside the point — it has the direction of the
result backwards. The finding was that entries DISAPPEAR, and sustained
gossip is what would PREVENT that. Probing occupies the daemon's serial
accept loop (measured: a median 5.2 s of every 8.2 s cycle, a ~64% duty
cycle), so it displaces the traffic that would keep an entry alive. A
heavily observed run is therefore biased *toward* the disappearance it
reported.

Hence `--control`: the same cell sampled THREE times — at startup, at a
propagation check, and at the end — against the treatment's dense
sampling (59 rows in the measured runs). It does not
eliminate observation artefacts, and must not be described as if it
did; whatever a single probe does is present in both arms. What it
bounds is the REPEATED load. If the entry is also absent at the end of a
lightly sampled run, sustained probing is not what made it disappear.

The propagation check is enforced, not just recorded: a control whose
middle sample does not find the entry at every node is written with
`record_valid: false` and exits non-zero, because "absent at the end" is
equally consistent with "never propagated". The confirmation must also
land BEFORE the author's own copy could expire — full presence after
that point is consistent with the entry having expired and come back,
which is the phenomenon under study rather than evidence of normal
propagation. `record_valid` additionally requires every observation in
the run to have succeeded, since the endpoint is the claim and a failed
final row would otherwise let an earlier row stand in for "the end".

Usage:
    python3 experiments/post_expiry.py                  # default cell
    python3 experiments/post_expiry.py --control
    python3 experiments/post_expiry.py --bleach-ttl 120 --window 900
    python3 experiments/post_expiry.py --reanalyse   # re-derive, no re-run
"""

import glob
import hashlib
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from harness import ensure_inner, Mesh, DEFAULT_BIN       # noqa: E402
import single_factor as sf                                # noqa: E402
import provenance                                         # noqa: E402

RESULTS = os.path.join(HERE, "results", "post-expiry")

# The +latency cell from divergence_dynamics: it produced 55.9 s per
# 100 s of divergence, so neighbours run meaningfully ahead of the
# author — which is the precondition for resurrection. On a fast bridge
# every node decays in lockstep and the entry would simply die.
CELL = {"nodes": 5, "interval": 5, "delay_ms": 400, "rate_kbit": 512,
        "entries": 25, "directed": False}

SAMPLE_GAP = 3.0

# When the control takes its propagation-confirmation sample. Must sit
# after the entry has reached every node and before the author's copy
# expires — which only exists as a window if the entry LIVES longer than
# it takes to spread. In the first cell it did not: with a 24 s insert
# TTL the entry reached all five nodes at t~28 s, when the author was
# already down to TTL 3. Propagation time ~= lifetime made that cell
# unfit for a question about what happens after expiry, so the default
# bleach TTL is now long enough to separate the two.
PROPAGATION_CHECK_T = 45.0


def _before_expiry(t, insert_ttl):
    """Did this confirmation land while the author still held the entry?

    A propagation check placed AFTER the author's copy could expire
    proves the wrong thing: full presence at that moment would be
    consistent with the entry having expired and come back — the very
    phenomenon under study — rather than with it having propagated
    normally. So confirmation is only accepted strictly before the
    author's own expiry.
    """
    return insert_ttl is not None and t is not None and t < insert_ttl


def _proc_alive(node):
    """Is the node's process still running? Diagnostic only, zero load.

    Liveness went through two wrong versions. First `wait_listening()`,
    which greps the log for an *earlier* "Listening" line — history, so a
    daemon that started and then died still read as alive, exactly the
    case the check existed to catch. Then a bare TCP connect, which was
    live but *added accept-loop load after every absent observation* —
    that is, precisely during the post-expiry period this experiment
    measures, and it did not even appear in `row_span_s`, since `done` is
    taken before it.

    So no extra connection is made at all. A probe that SUCCEEDS already
    proves the daemon responded; a probe that FAILS already invalidates
    the row. `proc.poll()` then only labels why, and touches nothing.
    """
    proc = getattr(node, "proc", None)
    if proc is None:
        return None
    return proc.poll() is None


def _sample(nodes, key, t0):
    """Presence AND absence of `key` at every node, probed concurrently.

    Deliberately not `divergence_dynamics._sample_row`, which returns
    None unless every node holds the key — it treats a missing entry as
    an unusable row, because it was written to measure spread among
    nodes that all have one. Here the missing entry IS the observation.
    """
    out = {}
    lock = threading.Lock()

    def one(n):
        tp = time.time()
        err = None
        e = None
        try:
            e = n.probe(sf.TYPE, timeout=180).get(key)
        except Exception as exc:                       # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"[:200]
        done = time.time()
        # A FAILED PROBE IS NOT AN ABSENT ENTRY. The first version
        # caught every exception and set e=None, which the analysis then
        # read as expiry — so a crashed or unreachable daemon would have
        # manufactured "mesh extinction", the headline result. Success
        # and failure are recorded separately, and a failure invalidates
        # the row rather than counting as absence.
        #
        # A SUCCESSFUL probe is itself the liveness evidence: the daemon
        # answered. Only a failure needs explaining, and then only from
        # `proc.poll()`, which costs the node nothing.
        alive = True if err is None else _proc_alive(n)
        with lock:
            out[n.name] = {"ttl": (e or {}).get("ttl"),
                           "ok": err is None,
                           "error": err,
                           "present": err is None and e is not None,
                           "alive": alive,
                           "gen": ((e or {}).get("data") or {}).get("gen"),
                           "start": round(tp - t0, 2),
                           "done": round(done - t0, 2)}

    threads = [threading.Thread(target=one, args=(n,)) for n in nodes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    errors = [nm for nm, v in out.items() if not v["ok"]]
    dead = [nm for nm, v in out.items() if v["alive"] is False]
    present = [v for v in out.values() if v["present"]]

    # First-order skew correction, same as divergence_dynamics: a TTL
    # read at t_i is normalised to the row's reference time. `spread_raw`
    # is kept so the correction's size stays visible. Using the raw value
    # was flagged as reintroducing the very instrument defect that
    # experiment exists to correct.
    ttls = [v["ttl"] for v in present]
    spread_raw = (max(ttls) - min(ttls)) if len(ttls) > 1 else None
    spread = None
    if len(present) > 1:
        t_ref = max(v["done"] for v in present)
        corr = [v["ttl"] - (t_ref - v["done"]) for v in present]
        spread = round(max(corr) - min(corr), 1)

    starts = [v["start"] for v in out.values()]
    dones = [v["done"] for v in out.values()]
    return {
        "t": round(time.time() - t0, 1),
        "present_count": len(present),
        "ttls": {nm: v["ttl"] for nm, v in out.items()},
        "spread": spread,
        "spread_raw": spread_raw,
        # A row is a SMEAR, not a snapshot: probes block on the daemon's
        # serial accept loop, so a row can span longer than the entry's
        # whole TTL. An all-absent row therefore means "each node was
        # absent at some point within this span", not "all were absent
        # together". Recorded per row so no reader has to take the word
        # "simultaneous" on trust.
        "row_span_s": round(max(dones) - min(starts), 1),
        "errors": errors,
        "dead_nodes": dead,
        "valid": not errors and not dead,
        "obs": out,
    }


def analyse(series, author_name, node_names):
    """Resurrection events and sampled presence, derived from the timeline.

    Only VALID rows are used — every probe succeeded and no node was
    found dead. An invalidated row is not evidence of anything, in
    either direction.

    EXCEPT for the endpoint, which is read from the ACTUAL last row.
    Dropping invalid rows and then taking `series[-1]` silently promotes
    an earlier row to "the end": a run whose final observation failed
    would report the state at some earlier time as its endpoint, without
    saying so. `absent_at_end` is therefore None when the real final row
    is invalid — unknown, not inherited.
    """
    full = series
    invalid = [r for r in series if not r["valid"]]
    series = [r for r in series if r["valid"]]

    events = []
    for nm in node_names:
        prev = None
        held_before = False
        for row in series:
            now = row["obs"][nm]["present"]
            # A resurrection is present -> absent -> PRESENT AGAIN.
            #
            # Counting a bare absent->present transition was wrong and
            # produced a giveaway constant: every run reported exactly 4
            # "resurrections", one per non-author node, because a node
            # that has not yet been reached is absent and then becomes
            # present — that is initial propagation, not resurrection.
            # The number never varied because it was counting the mesh
            # size, not the phenomenon.
            if prev is False and now is True and held_before:
                events.append({"node": nm, "t": row["t"],
                               "ttl_on_return": row["obs"][nm]["ttl"],
                               "gen": row["obs"][nm]["gen"]})
            if now:
                held_before = True

            prev = now

    # TTL RESETS — an absence-sample-independent lower-bound witness.
    #
    # The transition metric above only sees a resurrection when a sample
    # happens to land in the gap between expiry and return. Review found
    # it undercounting: with ~8 s between a node's own samples, an entry
    # can expire and be re-adopted unseen. What CANNOT hide from a pair
    # of present samples is the TTL going UP — a positive jump is
    # decisive without ever sampling the absence. It is still a lower
    # bound, not a census: sampling can miss a reset too (two expiries
    # between one pair of samples collapse into one jump, and a return
    # that decays below the pre-expiry trajectory before the next sample
    # shows no jump at all).
    #
    # While the author holds its own key, its TTL can only fall: bleach
    # decrements, and a higher own-authored value arriving from a peer is
    # discarded at sharedstate.cc:882 ("is remote peer ill?"). The author
    # publishes exactly once here. So an increase in the author's TTL is
    # only possible if the key left local state and came back through the
    # missing-key path at :866 — expiry plus re-adoption, whether or not
    # the absence itself was ever sampled.
    resets = []
    prev_row = None
    for row in series:
        cur = row["obs"][author_name]
        if prev_row is not None:
            was = prev_row["obs"][author_name]
            if (was["present"] and cur["present"]
                    and was["ttl"] is not None and cur["ttl"] is not None
                    and cur["ttl"] > was["ttl"]):
                resets.append({"t_prev": prev_row["t"], "t": row["t"],
                               "ttl_prev": was["ttl"], "ttl": cur["ttl"],
                               "delta": cur["ttl"] - was["ttl"],
                               "sampled_absence": False})
        prev_row = row

    # Per-node sampling gaps. The row span is how long ONE row takes;
    # what bounds the detector is how long between a given node's own
    # consecutive observations, which is larger.
    gaps = []
    for nm in node_names:
        ts = [r["obs"][nm]["done"] for r in series]
        gaps += [round(b - a, 2) for a, b in zip(ts, ts[1:])]
    gaps.sort()

    author_events = [e for e in events if e["node"] == author_name]
    all_absent = [r["t"] for r in series if r["present_count"] == 0]
    last_row = full[-1] if full else None
    final_absent = (last_row["present_count"] == 0
                    if (last_row and last_row["valid"]) else None)

    # Only counts if nothing was sampled present afterwards. This is
    # "no later sampled presence", NOT continuous quiescence: nodes are
    # sampled every ~8 s, so a resurrection shorter than that gap is
    # invisible to this instrument.
    no_later_presence_at = None
    if all_absent:
        last_present = [r["t"] for r in series if r["present_count"] > 0]
        if not last_present or all_absent[-1] > last_present[-1]:
            no_later_presence_at = next(t for t in all_absent
                              if not [p for p in last_present if p > t])

    spans = [r["row_span_s"] for r in series]
    return {
        # Renamed: this counts returns whose ABSENCE was sampled, which
        # is a lower bound on resurrections, not a count of them.
        "sampled_absence_returns_total": len(events),
        "sampled_absence_returns_at_author": len(author_events),
        "author_events": author_events,
        # Absence-sample-independent, and the number to quote.
        "ttl_reset_events": resets,
        "ttl_reset_count": len(resets),
        # SUM, not max: the two classes are disjoint by construction. A
        # sampled return requires the preceding author sample to be
        # ABSENT; a TTL reset requires both adjacent samples PRESENT.
        # One physical resurrection lands in exactly one class depending
        # on whether its absence was sampled, so max() double-discarded
        # whichever class was smaller and reported 2/2/2 where the data
        # support 2/3/3. Still a lower bound overall.
        "resurrection_evidence_count": len(author_events) + len(resets),
        "node_sample_gap_s_median": (gaps[len(gaps) // 2] if gaps else None),
        "node_sample_gap_s_max": (gaps[-1] if gaps else None),
        "author_max_ttl_on_return": (max((e["ttl_on_return"] or 0)
                                         for e in author_events)
                                     if author_events else None),
        # Named for what a smeared row can actually support. "Extinct"
        # asserted simultaneity these observations do not have: rows
        # span seconds to tens of seconds, sometimes longer than the
        # entry's whole TTL, so consecutive all-absent rows show
        # QUIESCENCE — nothing was found anywhere across many spans —
        # not a single instant at which the mesh emptied.
        "ever_all_absent_in_a_row": bool(all_absent),
        "first_all_absent_row_t": all_absent[0] if all_absent else None,
        "no_later_sampled_presence_from_t": no_later_presence_at,
        "absent_at_end": final_absent,
        "final_row_valid": bool(last_row and last_row["valid"]),
        "final_row_t": last_row["t"] if last_row else None,
        "max_spread": max((r["spread"] or 0) for r in series)
        if series else None,
        "max_spread_raw": max((r["spread_raw"] or 0) for r in series)
        if series else None,
        "row_span_s_max": max(spans) if spans else None,
        "row_span_s_median": (sorted(spans)[len(spans) // 2]
                              if spans else None),
        "samples_valid": len(series),
        "samples_invalidated": len(invalid),
        "invalidated_rows": [{"t": r["t"], "errors": r["errors"],
                              "dead_nodes": r["dead_nodes"]}
                             for r in invalid][:20],
    }


ANALYSIS_SCHEMA = 2


def analysis_provenance():
    """Identify the code that produced the CURRENT `analysis` fields.

    `--reanalyse` rewrites `analysis` in place from the stored series.
    The top-level `provenance` block still names the script that
    ACQUIRED the data — correct, and it must not change — but review
    found that then NOTHING identifies the analyser: the records carried
    the measurement-time script hash while the fields in them came from
    a later version of `analyse()`. This block travels with `analysis`
    and is refreshed whenever the analysis is."""
    with open(__file__, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return {"analyzer_sha256": digest,
            "analysis_schema": ANALYSIS_SCHEMA,
            "analysed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def run(window, bleach_ttl, binary, rundir, control=False):
    names = sf.NAMES[:CELL["nodes"]]
    deps_before = provenance.manifest(__file__)
    with Mesh(names, rundir, binary=binary) as mesh:
        offsets = mesh.stagger_clocks(CELL["interval"])
        nodes = mesh.bootstrap(sf.TYPE, update_interval=CELL["interval"],
                               bleach_ttl=bleach_ttl,
                               nodes=[mesh.node(n) for n in names],
                               full_mesh=False)
        mesh.chain(names=names, directed=CELL["directed"])
        for n in nodes:
            mesh.impair(n, delay_ms=CELL["delay_ms"],
                        rate_kbit=CELL["rate_kbit"])

        author = nodes[0]
        key = author.name
        # Bulk payload so a sync takes real time — transfer duration is
        # what inflates a neighbour's TTL above the author's.
        author.cli(f"insert {sf.TYPE}",
                   stdin=sf._bulk_payload(author.name, CELL["entries"]),
                   timeout=600)
        author.publish(sf.TYPE, key, gen=1, timeout=120)
        t0 = time.time()

        insert_ttl = None
        prop_row_t = None
        series = []
        if control:
            # LIGHT-SAMPLING CONTROL — three samples, not zero.
            #
            # It does NOT eliminate observation artefacts, and calling it
            # a "no-probe control" oversold it. What it bounds is the
            # REPEATED observer load: the treatment holds the daemon's
            # serial accept loop at a ~64% duty cycle, and that displaces
            # the gossip which would keep an entry alive, biasing toward
            # disappearance. Three samples cannot do that. Anything the
            # single act of probing does — at startup, at confirmation,
            # at the end — is still present in both arms.
            row = _sample(nodes, key, t0)
            if row["obs"][author.name]["ttl"]:
                insert_ttl = row["obs"][author.name]["ttl"]
            series.append(row)

            # PROPAGATION CONFIRMATION. Without this the control proves
            # only "absent at the end", which is also what a run where
            # the entry never propagated would show — and review found
            # exactly that: the first control row saw present_count=1,
            # so nothing established the entry ever reached the mesh.
            # One extra sample, placed after the entry should have
            # spread and before it can expire, turns "absent at the end"
            # into "propagated, then absent at the end". Three probes
            # total against the treatment's 59.
            wait = PROPAGATION_CHECK_T - (time.time() - t0)
            if wait > 0:
                time.sleep(wait)
            prop_row = _sample(nodes, key, t0)
            series.append(prop_row)
            prop_row_t = prop_row["t"]
            # ENFORCED, not merely recorded. A control whose middle
            # sample does not show the entry at every node has not shown
            # propagation, so its final absence is equally consistent
            # with an entry that never spread — the exact ambiguity this
            # sample was added to remove. Recording the condition and
            # then writing a successful-looking record regardless would
            # leave the reader to notice.
            propagated = (prop_row["valid"]
                          and prop_row["present_count"] == len(nodes)
                          and _before_expiry(prop_row["t"],
                                             bleach_ttl + CELL["interval"] + 1))

            remaining = window - (time.time() - t0)
            if remaining > 0:
                time.sleep(remaining)
            series.append(_sample(nodes, key, t0))
        else:
            while time.time() - t0 < window:
                row = _sample(nodes, key, t0)
                if insert_ttl is None and row["obs"][author.name]["ttl"]:
                    insert_ttl = row["obs"][author.name]["ttl"]
                series.append(row)
                time.sleep(SAMPLE_GAP)
            # The treatment samples densely, so propagation is confirmed
            # by the first valid row in which every node held the entry.
            confirming = next((r for r in series
                               if r["valid"] and r["present_count"] == len(nodes)
                               and _before_expiry(r["t"],
                                          bleach_ttl + CELL["interval"] + 1)), None)
            propagated = confirming is not None
            prop_row_t = confirming["t"] if confirming else None

    deps_after = provenance.manifest(__file__)
    return {
        "propagation_confirmed": propagated,
        "cell": dict(CELL, bleach_ttl=bleach_ttl),
        "window_s": window,
        "sample_gap_s": SAMPLE_GAP,
        "control_light_sampling": control,
        "author": author.name,
        "clock_offsets": offsets,
        # The CONFIGURED value: shared_state_cli.cc:66 assigns
        # mBleachTTL + mUpdateInterval + 1. Use this for lifetimes and
        # ratios. The observed one is lower simply because the first
        # probe lands after publication, and quoting it as the insert
        # TTL understated the lifetime by the startup delay.
        "configured_insert_ttl": bleach_ttl + CELL["interval"] + 1,
        "first_observed_author_ttl": insert_ttl,
        "series": series,
        "analysis": analyse(series, author.name, names),
        "analysis_provenance": analysis_provenance(),
        # Same check the other definitive experiments carry: a module
        # edited mid-run would otherwise be invisible, since modules are
        # imported once at startup.
        "deps_stable": deps_before == deps_after,
        "deps_changed": sorted(k for k in set(deps_before) | set(deps_after)
                               if deps_before.get(k) != deps_after.get(k)),
        # Every knob that shapes the experiment, in the record. A
        # setting that changes the result and lives only in the command
        # line is not reproducible from the evidence.
        "propagation_check_t_requested": PROPAGATION_CHECK_T,
        "propagation_confirmed_at_t": prop_row_t,
        # Single field a reader can filter on, so an unusable run cannot
        # be quoted by someone who did not check the parts. A failed
        # observation anywhere disqualifies a DEFINITIVE run: the
        # endpoint is the claim, and analyse() would otherwise promote an
        # earlier row to "the end" when the last one failed.
        "observations_valid": all(r["valid"] for r in series),
        "record_valid": (bool(propagated) and deps_before == deps_after
                         and all(r["valid"] for r in series)),
        "provenance": provenance.collect(binary, __file__),
    }


if __name__ == "__main__":
    def opt(name, default, cast=int):
        return cast(sys.argv[sys.argv.index(name) + 1]) \
            if name in sys.argv else default

    # --reanalyse is pure JSON-in, JSON-out and must run before
    # ensure_inner(): re-deriving stored analyses needs no namespace, no
    # network and no binary, and gating it on user-namespace support
    # would make the records unreadable exactly where they are most
    # likely to be read — on a reviewer's machine.
    if "--reanalyse" in sys.argv:
        os.makedirs(RESULTS, exist_ok=True)
        # The series is the measurement; `analysis` is only a reading of
        # it, so a corrected reading is applied to the data already
        # collected rather than used as a reason to re-run.
        #
        # Analyse-then-replace, in two strict passes. ALL records are
        # analysed and their report lines rendered BEFORE any file is
        # touched: the first version wrote each file as it went and then
        # crashed on a stale key in its own print, leaving one record on
        # the new schema and the rest on the old — from a tool whose
        # entire job is keeping the batch coherent. An analysis or
        # rendering failure now happens with every file still intact.
        # Each write then goes through os.replace, so no single file is
        # ever half-written; an I/O failure MID-LOOP can still leave the
        # batch split between old and new analyses (per-file atomic, not
        # batch-atomic) — re-running the reanalysis converges it.
        updated = []
        report = []
        for path in sorted(glob.glob(os.path.join(RESULTS, "*.json"))):
            with open(path) as f:
                rec = json.load(f)
            names = list(rec["series"][0]["obs"]) if rec["series"] else []
            rec["analysis"] = analyse(rec["series"], rec["author"], names)
            rec["analysis_provenance"] = analysis_provenance()
            a = rec["analysis"]
            report.append(
                f"{os.path.basename(path)}\n"
                f"  ttl_resets={a['ttl_reset_count']} "
                f"sampled_returns@author="
                f"{a['sampled_absence_returns_at_author']} "
                f"evidence>={a['resurrection_evidence_count']} "
                f"no_later_presence_from="
                f"{a['no_later_sampled_presence_from_t']}\n"
                f"  analyzer={rec['analysis_provenance']['analyzer_sha256'][:8]} "
                f"schema={rec['analysis_provenance']['analysis_schema']}")
            updated.append((path, rec))
        for path, rec in updated:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rec, f, indent=1, sort_keys=True)
            os.replace(tmp, path)
        print("\n".join(report))
        sys.exit(0)

    ensure_inner()   # re-exec into the unprivileged namespace

    window = opt("--window", 480)
    bleach_ttl = opt("--bleach-ttl", 90)
    # Impairment overrides: the precondition for resurrection is a
    # neighbour holding a MEANINGFULLY higher TTL when the author's copy
    # dies, so the interesting axis is how much inflation accumulates
    # within one entry lifetime.
    if "--rate-kbit" in sys.argv:
        CELL["rate_kbit"] = opt("--rate-kbit", CELL["rate_kbit"])
    if "--delay-ms" in sys.argv:
        CELL["delay_ms"] = opt("--delay-ms", CELL["delay_ms"])
    if "--entries" in sys.argv:
        CELL["entries"] = opt("--entries", CELL["entries"])
    if "--propagation-check-t" in sys.argv:
        # Settable so the gate can be exercised in BOTH directions. A
        # check that has only ever been seen to pass is not a verified
        # check; setting this before propagation completes is how the
        # refusal path gets tested.
        globals()["PROPAGATION_CHECK_T"] = opt("--propagation-check-t",
                                               PROPAGATION_CHECK_T, float)
    tag = opt("--tag", "", str)
    # DEFAULT_BIN, not a hand-counted path: dirname-counting from
    # experiments/ lands on <repo>/tests, the exact off-by-one that once
    # wrote a null harness into every provenance record.
    binary = opt("--bin", DEFAULT_BIN, str)
    os.makedirs(RESULTS, exist_ok=True)

    control = "--control" in sys.argv
    rec = run(window, bleach_ttl, binary, "/tmp/ss-post-expiry",
              control=control)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = os.path.join(
        RESULTS, f"post-expiry-ttl{bleach_ttl}{'-' + tag if tag else ''}"
                 f"{'-control' if control else ''}-{stamp}.json")
    with open(path, "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)

    a = rec["analysis"]
    print(f"\n{path}")
    print(f"  arm                     : "
          f"{'CONTROL (3 samples)' if control else 'treatment (dense)'}")
    print(f"  propagation confirmed   : {rec['propagation_confirmed']}"
          f"  (at t={rec['propagation_confirmed_at_t']}, "
          f"check requested t={rec['propagation_check_t_requested']})")
    print(f"  observations all valid  : {rec['observations_valid']}")
    print(f"  RECORD VALID            : {rec['record_valid']}")
    print(f"  insert TTL cfg/observed : {rec['configured_insert_ttl']}s / "
          f"{rec['first_observed_author_ttl']}s")
    print(f"  valid samples           : {a['samples_valid']} over {window}s"
          f"  (invalidated {a['samples_invalidated']})")
    print(f"  row span s (med / max)  : {a['row_span_s_median']} / "
          f"{a['row_span_s_max']}")
    print(f"  sampled absence returns : {a['sampled_absence_returns_at_author']}"
          f" (author) / {a['sampled_absence_returns_total']} (any)")
    print(f"  TTL RESET events        : {a['ttl_reset_count']}"
          f"  deltas={[e['delta'] for e in a['ttl_reset_events']]}")
    print(f"  node sample gap med/max : {a['node_sample_gap_s_median']} / "
          f"{a['node_sample_gap_s_max']}")
    print(f"  max TTL on return       : {a['author_max_ttl_on_return']}")
    print(f"  max spread (corr / raw) : {a['max_spread']} / "
          f"{a['max_spread_raw']}")
    print(f"  any all-absent row      : {a['ever_all_absent_in_a_row']}"
          f" (first at t={a['first_all_absent_row_t']})")
    print(f"  no later sampled presence from: "
          f"{a['no_later_sampled_presence_from_t']}")
    print(f"  absent at window end    : {a['absent_at_end']}")
    print(f"  deps stable             : {rec['deps_stable']}")
    if not rec["record_valid"]:
        print("\nRECORD IS NOT VALID — do not cite it. "
              + ("propagation was not confirmed (the entry was not seen at "
                 "every node in a valid sample), so a final absence says "
                 "nothing. " if not rec["propagation_confirmed"] else "")
              + ("the dependency manifest changed mid-run. "
                 if not rec["deps_stable"] else "")
              + ("at least one observation failed, so the endpoint cannot "
                 "be read from the row that was meant to be the end. "
                 if not rec["observations_valid"] else ""), file=sys.stderr)
        sys.exit(1)
