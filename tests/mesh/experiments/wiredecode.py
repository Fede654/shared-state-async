"""Offline pcap decoder for shared-state gossip (v2 plan, Phase 1).

Input: the per-node pcaps written by wirecap.py. Output: two linked
tables per the amended plan —

  frames  : one row per decoded protocol frame (request or response),
            with session identity, role, direction relative to the
            owning node, peer, type name, ENTRY COUNT, frame start and
            completion timestamps, application-level frame-ack status
            (spec §4: 4-byte BE total-bytes echo, load-bearing for
            session success — validated here, separately from TCP
            transport acknowledgments), and connection completion.
  entries : one row per payload item, foreign-keyed to its frame.

Absence is therefore a positive statement: a completed, acknowledged
full-state frame exists and the target key is not among its entries —
never "the decoder emitted no row".

Witness semantics (direction matters — an echo ARRIVING at the author
proves an offer, not acceptance): resurrection witnesses derive ONLY
from the author's OUTBOUND completed slices, taken from the author's
own pcap. Sessions with the harness host (the bridge address, used by
probes and injections) are excluded from witness extraction entirely;
they are ground truth for the validation run and nothing else.

Offline-only: this module never runs during collection.

Usage:
  python3 wiredecode.py --record <run-record.json>     # full pipeline
  python3 wiredecode.py --pcap <file.pcap> --node-ip <ip>  # one pcap
"""

import glob
import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wire                                              # noqa: E402

HOST_IP_SUFFIX = ".1"        # bridge address <subnet>.1 = probes/injections
TCP_PORT = 3490

DECODE_SCHEMA = 1


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- pcap layer ------------------------------------------------------------

def read_pcap(path):
    """Yield (ts_float, raw_frame_bytes). Classic pcap only (tcpdump -w)."""
    with open(path, "rb") as f:
        gh = f.read(24)
        if len(gh) < 24:
            return
        magic = struct.unpack("<I", gh[:4])[0]
        if magic == 0xa1b2c3d4:
            endian, div = "<", 1e6
        elif magic == 0xa1b23c4d:
            endian, div = "<", 1e9
        elif magic == 0xd4c3b2a1:
            endian, div = ">", 1e6
        elif magic == 0x4d3cb2a1:
            endian, div = ">", 1e9
        else:
            raise ValueError(f"{path}: not a pcap file (magic {magic:#x})")
        linktype = struct.unpack(endian + "I", gh[20:24])[0]
        if linktype != 1:                      # EN10MB
            raise ValueError(f"{path}: unsupported linktype {linktype}")
        while True:
            rh = f.read(16)
            if len(rh) < 16:
                return
            ts_s, ts_f, incl, orig = struct.unpack(endian + "IIII", rh)
            data = f.read(incl)
            if len(data) < incl:
                return                          # truncated tail
            if incl < orig:
                raise ValueError(f"{path}: snapped packet ({incl}<{orig}); "
                                 "capture was not full-snaplen")
            yield ts_s + ts_f / div, data


def parse_packet(raw):
    """Ethernet/IPv4/TCP → dict, or None for anything else."""
    if len(raw) < 34:
        return None
    if raw[12:14] != b"\x08\x00":              # IPv4 only
        return None
    ip = raw[14:]
    ihl = (ip[0] & 0x0F) * 4
    if ip[9] != 6:                             # TCP
        return None
    total = struct.unpack("!H", ip[2:4])[0]
    src = ".".join(str(b) for b in ip[12:16])
    dst = ".".join(str(b) for b in ip[16:20])
    tcp = ip[ihl:total]
    if len(tcp) < 20:
        return None
    sport, dport = struct.unpack("!HH", tcp[:4])
    seq = struct.unpack("!I", tcp[4:8])[0]
    doff = (tcp[12] >> 4) * 4
    flags = tcp[13]
    payload = tcp[doff:]
    return {"src": src, "dst": dst, "sport": sport, "dport": dport,
            "seq": seq, "flags": flags, "payload": payload,
            "syn": bool(flags & 0x02), "fin": bool(flags & 0x01),
            "rst": bool(flags & 0x04)}


# -- TCP reassembly --------------------------------------------------------

class Stream:
    """One direction of one connection: ordered bytes + per-byte times."""

    def __init__(self):
        self.isn = None
        self.segs = {}        # relative offset -> (bytes, ts of first sight)
        self.fin = False
        self.rst = False
        self.fin_ts = None

    def add(self, pkt, ts):
        if pkt["syn"]:
            self.isn = pkt["seq"]
            return
        if pkt["rst"]:
            self.rst = True
        if pkt["fin"]:
            self.fin = True
            self.fin_ts = self.fin_ts or ts
        if self.isn is None or not pkt["payload"]:
            return
        off = (pkt["seq"] - (self.isn + 1)) & 0xFFFFFFFF
        if off > 1 << 30:
            return                              # wildly out of window
        if off not in self.segs:                # keep FIRST arrival
            self.segs[off] = (pkt["payload"], ts)

    def assemble(self):
        """Returns (bytes, offset->ts list, gap boolean).

        A gap = a hole in the byte stream with data beyond it — the
        reassembly failure the completeness gate looks for. Overlapping
        retransmissions are trimmed; the first-seen timestamp is kept
        per byte range.
        """
        data = bytearray()
        times = []            # (start_offset_in_data, ts)
        gap = False
        expected = 0
        for off in sorted(self.segs):
            payload, ts = self.segs[off]
            if off > expected:
                gap = True
                break
            skip = expected - off
            if skip >= len(payload):
                continue
            times.append((len(data), ts))
            data += payload[skip:]
            expected = off + len(payload)
        return bytes(data), times, gap

    @staticmethod
    def ts_at(times, offset):
        """Timestamp of the segment containing byte `offset`."""
        ts = None
        for start, t in times:
            if start <= offset:
                ts = t
            else:
                break
        return ts


def sessions_from_pcap(path):
    """Group packets into connections keyed by (client_ip, client_port)."""
    conns = {}
    for ts, raw in read_pcap(path):
        pkt = parse_packet(raw)
        if pkt is None:
            continue
        if pkt["dport"] == TCP_PORT:
            key = (pkt["src"], pkt["sport"], pkt["dst"])
            direction = "c2s"
        elif pkt["sport"] == TCP_PORT:
            key = (pkt["dst"], pkt["dport"], pkt["src"])
            direction = "s2c"
        else:
            continue
        conn = conns.setdefault(key, {"c2s": Stream(), "s2c": Stream(),
                                      "first_ts": ts})
        conn[direction].add(pkt, ts)
    return conns


# -- protocol layer --------------------------------------------------------

def _take_frame(buf, times, offset):
    """Decode one frame at `offset`. Returns (frame_dict, end) or (None, why)."""
    if len(buf) < offset + 1:
        return None, "truncated-before-frame"
    nlen = buf[offset]
    hdr_end = offset + 1 + nlen + 4
    if not 1 <= nlen <= wire.DATA_TYPE_NAME_MAX:
        return None, f"bad-name-length-{nlen}"
    if len(buf) < hdr_end:
        return None, "truncated-header"
    (dlen,) = struct.unpack("!I", buf[offset + 1 + nlen:hdr_end])
    if not 2 <= dlen <= wire.DATA_MAX:
        return None, f"bad-data-length-{dlen}"
    end = hdr_end + dlen
    if len(buf) < end:
        return None, "truncated-payload"
    name = buf[offset + 1:offset + 1 + nlen].decode(errors="replace")
    payload = buf[hdr_end:end]
    return {"type_name": name, "payload": payload,
            "wire_len": end - offset,
            "start_ts": Stream.ts_at(times, offset),
            "end_ts": Stream.ts_at(times, end - 1)}, end


def decode_session(conn):
    """Decode one connection into request/response frame dicts.

    Stream layout (spec §3–§5, mirrored in wire.client_session):
      c2s: [4B version][4B version echo][request frame][4B ack of response]
      s2c: [4B version][4B ack of request][response frame]
    """
    c2s, c2s_times, c2s_gap = conn["c2s"].assemble()
    s2c, s2c_times, s2c_gap = conn["s2c"].assemble()
    out = {"reassembly_gap": c2s_gap or s2c_gap,
           "rst": conn["c2s"].rst or conn["s2c"].rst,
           "fin_both": conn["c2s"].fin and conn["s2c"].fin,
           "first_ts": conn["first_ts"],
           "request": None, "response": None,
           "handshake_ok": None, "decode_error": None}
    if len(c2s) < 8 or len(s2c) < 4:
        out["decode_error"] = "handshake-incomplete"
        return out
    out["handshake_ok"] = (c2s[0:4] == s2c[0:4] == c2s[4:8])

    req, req_end = _take_frame(c2s, c2s_times, 8)
    if req is None:
        out["decode_error"] = f"request:{req_end}"
        return out
    # ack of request: first 4 bytes of s2c after version
    if len(s2c) >= 8:
        (ack_req,) = struct.unpack("!I", s2c[4:8])
        req["ack_value"] = ack_req
        req["ack_ok"] = ack_req == req["wire_len"]
        req["ack_ts"] = Stream.ts_at(s2c_times, 7)
    else:
        req["ack_value"], req["ack_ok"], req["ack_ts"] = None, False, None
    out["request"] = req

    resp, resp_end = _take_frame(s2c, s2c_times, 8)
    if resp is None:
        out["decode_error"] = f"response:{resp_end}"
        return out
    # ack of response: 4 bytes of c2s after the request frame
    if len(c2s) >= req_end + 4:
        (ack_resp,) = struct.unpack("!I", c2s[req_end:req_end + 4])
        resp["ack_value"] = ack_resp
        resp["ack_ok"] = ack_resp == resp["wire_len"]
        resp["ack_ts"] = Stream.ts_at(c2s_times, req_end + 3)
    else:
        resp["ack_value"], resp["ack_ok"], resp["ack_ts"] = None, False, None
    out["response"] = resp

    out["leftover_c2s"] = len(c2s) - (req_end + 4)
    out["leftover_s2c"] = len(s2c) - resp_end
    return out


# -- table builder ---------------------------------------------------------

def decode_node_pcap(path, node_ip, ip_names=None):
    """One node's pcap → (frames, entries, completeness).

    Direction is relative to the pcap's owning node (`node_ip`): a frame
    the node serialized and sent is `outbound` — the only kind witness
    extraction may use. The harness host (bridge, <subnet>.1) marks
    probe/injection sessions.
    """
    ip_names = ip_names or {}
    frames, entries = [], []
    completeness = {"pcap": path, "pcap_sha256": _sha256_file(path),
                    "sessions": 0, "reassembly_gaps": 0, "decode_errors": 0,
                    "handshake_failures": 0, "ack_failures": 0}
    subnet_host = node_ip.rsplit(".", 1)[0] + HOST_IP_SUFFIX

    conns = sessions_from_pcap(path)
    for (cip, cport, sip), conn in sorted(conns.items(),
                                          key=lambda kv: kv[1]["first_ts"]):
        completeness["sessions"] += 1
        sess = decode_session(conn)
        session_id = f"{cip}:{cport}->{sip}:{TCP_PORT}@{conn['first_ts']:.6f}"
        peer_ip = sip if cip == node_ip else cip
        is_host = peer_ip == subnet_host
        conn_complete = (sess["fin_both"] and not sess["rst"]
                         and not sess["reassembly_gap"]
                         and sess["decode_error"] is None
                         and sess.get("leftover_c2s") == 0
                         and sess.get("leftover_s2c") == 0)
        if sess["reassembly_gap"]:
            completeness["reassembly_gaps"] += 1
        if sess["decode_error"]:
            completeness["decode_errors"] += 1
        if sess["handshake_ok"] is False:
            completeness["handshake_failures"] += 1

        for role in ("request", "response"):
            fr = sess[role]
            if fr is None:
                continue
            sender_ip = cip if role == "request" else sip
            direction = "outbound" if sender_ip == node_ip else "inbound"
            payload_err = None
            slice_ = {}
            try:
                slice_ = wire.payload_decode(fr["payload"])
            except Exception as exc:                    # noqa: BLE001
                payload_err = f"{type(exc).__name__}: {exc}"[:120]
            if fr.get("ack_ok") is False:
                completeness["ack_failures"] += 1
            frame_id = f"{session_id}/{role}"
            frames.append({
                "frame_id": frame_id, "session_id": session_id,
                "node_ip": node_ip, "role": role, "direction": direction,
                "peer_ip": peer_ip,
                "peer": ("HOST" if is_host
                         else ip_names.get(peer_ip, peer_ip)),
                "is_host_session": is_host,
                "type_name": fr["type_name"],
                "entry_count": len(slice_),
                "frame_start_ts": fr["start_ts"],
                "frame_end_ts": fr["end_ts"],
                "app_ack_value": fr.get("ack_value"),
                "app_ack_expected": fr["wire_len"],
                "app_ack_ok": fr.get("ack_ok"),
                "app_ack_ts": fr.get("ack_ts"),
                "conn_complete": conn_complete,
                "payload_error": payload_err,
            })
            for key, e in slice_.items():
                entries.append({
                    "frame_id": frame_id, "key": key,
                    "author": e.get("author"), "ttl": e.get("ttl"),
                    "gen": (e.get("data") or {}).get("gen")
                    if isinstance(e.get("data"), dict) else None,
                    "version": e.get("version"),
                })
    return frames, entries, completeness


# -- witnesses (outbound-only, author's own pcap) --------------------------

def qualifying_outbound(frames, entries, author_ip, key):
    """The author's completed, acknowledged, non-host outbound full-state
    slices, in time order, each annotated with the target key's presence
    and TTL. This is the only witness source the plan permits."""
    by_frame = {}
    for e in entries:
        if e["key"] == key:
            by_frame[e["frame_id"]] = e
    rows = []
    for f in frames:
        if (f["node_ip"] == author_ip and f["direction"] == "outbound"
                and not f["is_host_session"] and f["conn_complete"]
                and f["app_ack_ok"] and f["payload_error"] is None):
            e = by_frame.get(f["frame_id"])
            rows.append({"frame_id": f["frame_id"],
                         "t": f["frame_end_ts"],
                         "present": e is not None,
                         "ttl": e["ttl"] if e else None,
                         "entry_count": f["entry_count"]})
    rows.sort(key=lambda r: r["t"] or 0)
    return rows


def extract_witnesses(outbound_rows):
    """Resurrection witnesses per the amended plan, from outbound only.

    - absence→presence: a completed outbound slice without the key,
      followed by one with it (single-publish is the runner's context,
      recorded in the run record, not re-derived here).
    - strict TTL increase: consecutive present outbound slices where
      the later TTL is strictly greater than the earlier one.
    """
    absret, ttlinc = [], []
    prev = None
    seen_present = False
    seen_absent_after_present = False
    for row in outbound_rows:
        if row["present"]:
            if seen_absent_after_present:
                absret.append({"t": row["t"], "ttl": row["ttl"],
                               "frame_id": row["frame_id"]})
                seen_absent_after_present = False
            if (prev is not None and prev["present"]
                    and prev["ttl"] is not None and row["ttl"] is not None
                    and row["ttl"] > prev["ttl"]):
                ttlinc.append({"t_prev": prev["t"], "t": row["t"],
                               "ttl_prev": prev["ttl"], "ttl": row["ttl"],
                               "delta": row["ttl"] - prev["ttl"]})
            seen_present = True
        else:
            if seen_present:
                seen_absent_after_present = True
        prev = row
    return {"outbound_absence_returns": absret,
            "outbound_ttl_increases": ttlinc,
            "witness_count": len(absret) + len(ttlinc)}


# -- flow verification (HRM-148) -------------------------------------------

def observed_edges(all_frames):
    """Unordered node-pairs with at least one non-host session, plus the
    set of ordered (client→server) pairs — 'both directions' means every
    declared edge produced sessions initiated from each side."""
    ordered = set()
    for f in all_frames:
        if f["is_host_session"]:
            continue
        if f["role"] == "request" and f["direction"] == "outbound":
            ordered.add((f["node_ip"], f["peer_ip"]))
    unordered = {tuple(sorted(p)) for p in ordered}
    return unordered, ordered


def verify_topology(all_frames, declared_edges_ips):
    """declared_edges_ips: set of (ip_a, ip_b) unordered tuples."""
    declared = {tuple(sorted(e)) for e in declared_edges_ips}
    unordered, ordered = observed_edges(all_frames)
    both_dirs = all(
        (a, b) in ordered and (b, a) in ordered for a, b in declared)
    return {"declared": sorted(declared), "observed": sorted(unordered),
            "match": unordered == declared, "both_directions": both_dirs,
            "ok": unordered == declared and both_dirs}


# -- record pipeline -------------------------------------------------------

def decode_record(record_path):
    """Full pipeline for one v2 run record → <record>.decode.json."""
    with open(record_path) as f:
        rec = json.load(f)
    cap = rec["capture"]
    ip_names = {v: k for k, v in rec["node_ips"].items()}
    author = rec["author"]
    author_ip = rec["node_ips"][author]
    key = author

    all_frames, all_entries, per_node = [], [], {}
    frames_by_node, entries_by_node = {}, {}
    for node, meta in cap["nodes"].items():
        frames, entries, comp = decode_node_pcap(
            meta["pcap"], rec["node_ips"][node], ip_names)
        comp["recorded_sha256_match"] = (
            comp["pcap_sha256"] == meta.get("pcap_sha256"))
        per_node[node] = comp
        for fr in frames:
            fr["node"] = node
        frames_by_node[node] = frames
        entries_by_node[node] = entries
        all_frames += frames
        all_entries += entries

    # Dedup for mesh-wide tables: every gossip session appears in both
    # endpoints' pcaps. Keep the copy owned by the session's CLIENT node
    # (arbitrary but deterministic); witness extraction below does NOT
    # use the deduped view — it reads the author's own pcap only.
    seen = {}
    for fr in all_frames:
        k = (fr["session_id"], fr["role"])
        if k not in seen or fr["node_ip"] == fr["session_id"].split(":")[0]:
            seen[k] = fr
    deduped_frames = list(seen.values())

    outbound = qualifying_outbound(frames_by_node[author],
                                   entries_by_node[author],
                                   author_ip, key)
    witnesses = extract_witnesses(outbound)

    declared = rec.get("declared_edges_ips")
    topo = (verify_topology(deduped_frames, [tuple(e) for e in declared])
            if declared else None)

    # Propagation from the wire: every non-author node emitted a
    # completed outbound slice containing the key before the author's
    # configured insert TTL elapsed (t0 from the run record).
    t0 = rec["t0_unix"]
    ttl_cfg = rec["configured_insert_ttl"]
    prop = {}
    for node, ip in rec["node_ips"].items():
        if node == author:
            continue
        rows = qualifying_outbound(frames_by_node[node],
                                   entries_by_node[node], ip, key)
        first = next((r for r in rows if r["present"]), None)
        prop[node] = (round(first["t"] - t0, 1)
                      if first and first["t"] else None)
    propagated = all(v is not None and v < ttl_cfg for v in prop.values())

    # End-of-window outbound coverage: an absence claim at window end
    # needs outbound evidence there, not silence. Require at least one
    # completed author outbound slice in the final 2 gossip intervals.
    interval = rec["cell"]["interval"]
    window_end = t0 + rec["window_s"]
    tail = [r for r in outbound
            if r["t"] and r["t"] >= window_end - 2 * interval]

    completeness_ok = (
        cap.get("capture_ok", False)
        and all(c["reassembly_gaps"] == 0 and c["recorded_sha256_match"]
                for c in per_node.values()))

    out = {
        "decode_schema": DECODE_SCHEMA,
        "record": os.path.basename(record_path),
        "decoder_sha256": _sha256_file(os.path.abspath(__file__)),
        "per_node_completeness": per_node,
        "frames_total": len(deduped_frames),
        "entries_total": len(all_entries),
        "author_outbound_qualifying": len(outbound),
        "author_outbound_rows": outbound,
        "witnesses": witnesses,
        "topology_verification": topo,
        "propagation_first_outbound_s": prop,
        "propagation_confirmed_wire": propagated,
        "tail_outbound_slices": len(tail),
        "tail_coverage_ok": len(tail) >= 1,
        "completeness_ok": completeness_ok,
        "decode_valid": (completeness_ok and propagated
                         and len(tail) >= 1
                         and (topo is None or topo["ok"])),
        "frames": deduped_frames,
        # entry rows only for the target key, mesh-wide; full per-frame
        # counts live in the frame rows. Complete entry tables are
        # re-derivable from the hashed pcaps at any time.
        "entries_target_key": [e for e in all_entries if e["key"] == key],
    }
    dest = record_path.replace(".json", "") + ".decode.json"
    tmp = dest + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    os.replace(tmp, dest)
    return dest, out


if __name__ == "__main__":
    if "--record" in sys.argv:
        for path in sorted(sys.argv[sys.argv.index("--record") + 1:]):
            for rp in glob.glob(path) or [path]:
                dest, out = decode_record(rp)
                w = out["witnesses"]
                print(f"{dest}\n"
                      f"  sessions/frames/entries: "
                      f"{sum(c['sessions'] for c in out['per_node_completeness'].values())}"
                      f"/{out['frames_total']}/{out['entries_total']}\n"
                      f"  author outbound qualifying: "
                      f"{out['author_outbound_qualifying']}\n"
                      f"  witnesses: {w['witness_count']} "
                      f"(absence-returns {len(w['outbound_absence_returns'])}, "
                      f"ttl-increases {len(w['outbound_ttl_increases'])})\n"
                      f"  propagation(wire): {out['propagation_confirmed_wire']}"
                      f"  topology: "
                      f"{(out['topology_verification'] or {}).get('ok')}"
                      f"  completeness: {out['completeness_ok']}"
                      f"  DECODE VALID: {out['decode_valid']}")
    elif "--pcap" in sys.argv:
        p = sys.argv[sys.argv.index("--pcap") + 1]
        ip = sys.argv[sys.argv.index("--node-ip") + 1]
        frames, entries, comp = decode_node_pcap(p, ip)
        print(json.dumps({"completeness": comp, "frames": len(frames),
                          "entries": len(entries)}, indent=1))
        for f in frames[:10]:
            print(f"{f['frame_start_ts']:.3f} {f['direction']:8s} "
                  f"{f['role']:8s} peer={f['peer']} n={f['entry_count']} "
                  f"ack={f['app_ack_ok']} complete={f['conn_complete']}")
    else:
        print(__doc__)
