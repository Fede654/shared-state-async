#!/usr/bin/env python3
"""Decode + analysis for the fleet experiments (HRM-152).

Reuses wiredecode's primitives (schema-4 pcap reader, incarnation
splitting, frame/entry/session tables) but classifies anomalies for
runs where mid-run session failures are EXPECTED:

  teardown     last packet within one interval of daemon stop
  disruption   last packet within a declared upgrade/reboot window
               (± one interval of grace)
  interop      session between two nodes running DIFFERENT binary
               versions at that moment (rolling upgrade only) — this
               class is the T23 measurement itself, reported, never
               invalidating
  unattributed anything else — invalidates the run

Analyses:
  coverage (rolling upgrade): per node over time, the set of AUTHORS
  present in its outbound completed slices — the mesh's knowledge map.
  T23 predicts upgraded nodes lose v1-authored coverage while v1 nodes
  keep everything.

  leapfrog (reboot storm): per reboot cycle of the target, its first
  own-key outbound slice after restart (gen, version) vs the last gen
  it published before the reboot; plus neighbour sightings of a
  regressed gen at elevated version.

Usage: python3 fleet_decode.py <run-record.json> [...]
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wiredecode as wd                                   # noqa: E402

FLEET_SCHEMA = 1


def node_version_at(rec, node, t_unix):
    """'v1' or 'v2' for rolling-upgrade records; 'v2' for storms."""
    if rec["experiment"] != "rolling-upgrade":
        return "v2"
    for ev in rec["upgrade_events"]:
        if ev["node"] == node and t_unix >= ev["t_started_unix"]:
            return "v2"
    return "v1"


def classify_anomalies(rec, all_sessions, all_frames, ip_names):
    interval = rec["cell"]["interval"]
    stop_t = rec["daemons_stopped_at_unix"]
    windows = [(a - interval, b + interval)
               for a, b in rec.get("expected_disruption_windows", [])]

    def in_window(ts):
        return any(a <= ts <= b for a, b in windows)

    out = {"teardown": 0, "disruption": 0, "interop": [],
           "unattributed": []}
    for s in all_sessions:
        anomalous = (s["handshake_ok"] is False
                     or s["decode_error"] is not None
                     or not s["conn_complete"])
        if not anomalous:
            continue
        sid = s["session_id"]
        if s["handshake_ok"] is False:
            out["unattributed"].append(f"{sid}@{s['node']}:handshake")
            continue
        cip = sid.split(":")[0]
        sip = sid.split("->")[1].split(":")[0]
        va = node_version_at(rec, ip_names.get(cip, cip), s["last_ts"])
        vb = node_version_at(rec, ip_names.get(sip, sip), s["last_ts"])
        if va != vb:
            out["interop"].append(
                {"session": f"{sid}@{s['node']}",
                 "t": round(s["last_ts"] - rec["t0_unix"], 1),
                 "client_ver": va, "server_ver": vb,
                 "error": s["decode_error"]})
        elif s["last_ts"] >= stop_t - interval:
            out["teardown"] += 1
        elif in_window(s["last_ts"]):
            out["disruption"] += 1
        else:
            out["unattributed"].append(f"{sid}@{s['node']}")
    return out


def decode_all(rec):
    ip_names = {v: k for k, v in rec["node_ips"].items()}
    frames_by_node, entries_by_node = {}, {}
    all_sessions, all_frames, per_node = [], [], {}
    for node, meta in rec["capture"]["nodes"].items():
        sessions, frames, entries, comp = wd.decode_node_pcap(
            meta["pcap"], rec["node_ips"][node], ip_names)
        comp["recorded_sha256_match"] = (
            comp["pcap_sha256"] == meta.get("pcap_sha256"))
        per_node[node] = comp
        for x in frames:
            x["node"] = node
        for x in sessions:
            x["node"] = node
        frames_by_node[node] = frames
        entries_by_node[node] = entries
        all_sessions += sessions
        all_frames += frames
    return (frames_by_node, entries_by_node, all_sessions, all_frames,
            per_node, ip_names)


def outbound_rows(rec, frames, entries, node):
    """Completed, acked, non-host outbound frames of `node`, each with
    its full author->(*gen, version) map, time-ordered."""
    ip = rec["node_ips"][node]
    by_frame = {}
    for e in entries:
        by_frame.setdefault(e["frame_id"], {})[e["key"]] = e
    rows = []
    for f in frames:
        if (f["node_ip"] == ip and f["direction"] == "outbound"
                and not f["is_host_session"] and f["conn_complete"]
                and f["app_ack_ok"] and f["payload_error"] is None
                and f["frame_end_ts"] is not None):
            rows.append({"t": round(f["frame_end_ts"] - rec["t0_unix"], 1),
                         "entries": by_frame.get(f["frame_id"], {})})
    rows.sort(key=lambda r: r["t"])
    return rows


def coverage_series(rec, frames_by_node, entries_by_node, bucket_s=30):
    """Per node: [(t_bucket, sorted authors carried)] from outbound."""
    out = {}
    for node in rec["node_ips"]:
        rows = outbound_rows(rec, frames_by_node[node],
                             entries_by_node[node], node)
        buckets = {}
        for r in rows:
            b = int(r["t"] // bucket_s) * bucket_s
            buckets.setdefault(b, set()).update(
                e["author"] for e in r["entries"].values()
                if e["author"])
        out[node] = [(b, sorted(a)) for b, a in sorted(buckets.items())]
    return out


def coverage_summary(rec, cov):
    """Author-count matrix at each bucket + last sighting per author."""
    lines = []
    names = list(rec["node_ips"])
    all_buckets = sorted({b for series in cov.values() for b, _ in series})
    lines.append("bucket_s " + " ".join(f"{n[:6]:>7s}" for n in names))
    for b in all_buckets:
        row = []
        for n in names:
            d = dict(cov[n])
            row.append(f"{len(d.get(b, [])):7d}" if b in d else f"{'-':>7s}")
        lines.append(f"{b:8d} " + " ".join(row))
    last_seen = {}
    for n in names:
        seen = {}
        for b, authors in cov[n]:
            for a in authors:
                seen[a] = b
        last_seen[n] = seen
    return "\n".join(lines), last_seen


def leapfrog_analysis(rec, frames_by_node, entries_by_node):
    """Per reboot cycle: target's first own-key outbound after restart."""
    target = rec["target"]
    key = target
    rows = outbound_rows(rec, frames_by_node[target],
                         entries_by_node[target], target)
    own = [(r["t"], r["entries"].get(key)) for r in rows
           if key in r["entries"]]
    cycles = []
    for ev in rec["reboot_events"]:
        after = [(t, e) for t, e in own if t >= ev["t_started"]]
        first = after[0] if after else None
        nxt_pub = next((p["t"] for p in rec["publish_log"]
                        if p.get("node") == target and p.get("ok")
                        and p["t"] > ev["t_started"]), None)
        c = {"cycle": ev["cycle"], "t_reboot": ev["t_started"],
             "gen_before": ev["last_published_gen_before"],
             "next_publish_t": nxt_pub}
        if first:
            t, e = first
            c.update({"first_own_out_t": t, "gen": e["gen"],
                      "version": e["version"],
                      "gen_regressed": (e["gen"] is not None
                                        and e["gen"] <
                                        ev["last_published_gen_before"]),
                      "before_next_publish": (nxt_pub is None
                                              or t < nxt_pub)})
        else:
            c["first_own_out_t"] = None
        cycles.append(c)

    # Neighbour sightings of regressed gens at/above the target's
    # pre-reboot version — the propagation half of the leapfrog.
    sightings = []
    for node in rec["node_ips"]:
        if node == target:
            continue
        for r in outbound_rows(rec, frames_by_node[node],
                               entries_by_node[node], node):
            e = r["entries"].get(key)
            if not e or e["gen"] is None:
                continue
            for c in cycles:
                if (c.get("gen_regressed") and r["t"] > c["t_reboot"]
                        and e["gen"] == c["gen"]
                        and (c["next_publish_t"] is None
                             or r["t"] < c["next_publish_t"] + 10)):
                    sightings.append({"node": node, "t": r["t"],
                                      "cycle": c["cycle"],
                                      "gen": e["gen"],
                                      "version": e["version"]})
                    break
    return cycles, sightings


def main(record_path):
    with open(record_path) as f:
        rec = json.load(f)
    (frames_by_node, entries_by_node, all_sessions, all_frames,
     per_node, ip_names) = decode_all(rec)

    anomalies = classify_anomalies(rec, all_sessions, all_frames, ip_names)
    completeness_ok = (
        rec["capture"].get("capture_ok", False)
        and all(c["reassembly_gaps"] == 0 and c["recorded_sha256_match"]
                for c in per_node.values())
        and not anomalies["unattributed"])

    out = {"fleet_schema": FLEET_SCHEMA,
           "record": os.path.basename(record_path),
           "decoder_sha256": wd._sha256_file(
               os.path.join(HERE, "wiredecode.py")),
           "self_sha256": wd._sha256_file(os.path.abspath(__file__)),
           "per_node_completeness": per_node,
           "anomalies": {"teardown": anomalies["teardown"],
                         "disruption": anomalies["disruption"],
                         "interop_failures": len(anomalies["interop"]),
                         "unattributed": anomalies["unattributed"]},
           "interop_failures": anomalies["interop"][:200],
           "completeness_ok": completeness_ok,
           "decode_valid": completeness_ok}

    if rec["experiment"] == "rolling-upgrade":
        cov = coverage_series(rec, frames_by_node, entries_by_node)
        matrix, last_seen = coverage_summary(rec, cov)
        out["coverage_matrix"] = matrix
        out["last_seen_author_by_node"] = last_seen
        out["coverage_series"] = {n: s for n, s in cov.items()}
    else:
        cycles, sightings = leapfrog_analysis(rec, frames_by_node,
                                              entries_by_node)
        out["leapfrog_cycles"] = cycles
        out["stale_gen_neighbour_sightings"] = sightings

    dest = record_path.replace(".json", "") + ".fleet.json"
    tmp = dest + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    os.replace(tmp, dest)

    print(f"{dest}")
    print(f"  anomalies: teardown {anomalies['teardown']}, disruption "
          f"{anomalies['disruption']}, interop "
          f"{len(anomalies['interop'])}, unattributed "
          f"{len(anomalies['unattributed'])}")
    print(f"  DECODE VALID: {out['decode_valid']}")
    if rec["experiment"] == "rolling-upgrade":
        print("  coverage matrix (authors carried per node, per 30 s "
              "bucket):")
        print("    " + out["coverage_matrix"].replace("\n", "\n    "))
    else:
        for c in out["leapfrog_cycles"]:
            print(f"  cycle {c['cycle']}: reboot t={c['t_reboot']} "
                  f"gen_before={c['gen_before']} -> first own out "
                  f"t={c.get('first_own_out_t')} gen={c.get('gen')} "
                  f"v={c.get('version')} regressed="
                  f"{c.get('gen_regressed')} before_next_publish="
                  f"{c.get('before_next_publish')}")
        print(f"  stale-gen neighbour sightings: "
              f"{len(out['stale_gen_neighbour_sightings'])}")
    return out


if __name__ == "__main__":
    for p in sys.argv[1:]:
        main(p)
