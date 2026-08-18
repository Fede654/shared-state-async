"""Synthetic selftests for wiredecode's audited edge cases.

Both defects below shipped in a frozen decoder and were caught by
external audit with synthetic pcaps; these tests reproduce those pcaps
so the defects cannot return silently:

1. Four-tuple reuse: two TCP incarnations on the same client
   (ip, port) must decode as two distinct connections keyed by ISN,
   each with its own payload — not one aggregate stamped with the last
   ISN and holding the first incarnation's bytes.
2. Frame-less failure: a connection that dies before any frame decodes
   must still reach the validity gate — unattributed when it fails
   mid-run, tolerated only when teardown-attributable.

Run: python3 wiredecode.py selftest  (or python3 selftest_wiredecode.py)
"""

import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wire                                              # noqa: E402
import wiredecode as wd                                  # noqa: E402

CLIENT = "10.99.0.12"
SERVER = "10.99.0.11"
CPORT = 40000


def _pkt(src, dst, sport, dport, seq, payload=b"", syn=False, fin=False):
    flags = (0x02 if syn else 0) | (0x01 if fin else 0) | 0x10
    tcp = (struct.pack("!HHIIBBHHH", sport, dport, seq, 0,
                       5 << 4, flags, 65535, 0, 0) + payload)
    total = 20 + len(tcp)
    ip = (struct.pack("!BBHHHBBH", 0x45, 0, total, 0, 0, 64, 6, 0)
          + bytes(int(b) for b in src.split("."))
          + bytes(int(b) for b in dst.split(".")))
    eth = b"\x00" * 12 + b"\x08\x00"
    return eth + ip + tcp


def _write_pcap(path, packets):
    """packets: list of (ts, raw)."""
    with open(path, "wb") as f:
        f.write(struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 262144, 1))
        for ts, raw in packets:
            sec, usec = int(ts), int((ts % 1) * 1e6)
            f.write(struct.pack("<IIII", sec, usec, len(raw), len(raw)))
            f.write(raw)


def _session_packets(t, isn_c, isn_s, gen, complete=True):
    """One full sync session's packets (client side), starting at time t."""
    ver = struct.pack("!I", 1)
    req_payload = wire.payload_encode(
        {"k": {"author": "n", "ttl": 30, "data": {"gen": gen}}})
    req = wire.encode_frame("t", req_payload)
    resp = wire.encode_frame("t", req_payload)
    pkts = []
    s = isn_c
    pkts.append((t, _pkt(CLIENT, SERVER, CPORT, 3490, s, syn=True)))
    pkts.append((t + .001, _pkt(SERVER, CLIENT, 3490, CPORT, isn_s,
                                syn=True)))
    c2s = ver + ver + req + wire.ack_for(resp)
    s2c = ver + wire.ack_for(req) + resp
    if not complete:
        c2s = c2s[:6]        # dies inside the handshake, no frame
        s2c = s2c[:4]
    pkts.append((t + .01, _pkt(CLIENT, SERVER, CPORT, 3490, s + 1, c2s)))
    pkts.append((t + .02, _pkt(SERVER, CLIENT, 3490, CPORT, isn_s + 1, s2c)))
    if complete:
        pkts.append((t + .03, _pkt(CLIENT, SERVER, CPORT, 3490,
                                   s + 1 + len(c2s), fin=True)))
        pkts.append((t + .04, _pkt(SERVER, CLIENT, 3490, CPORT,
                                   isn_s + 1 + len(s2c), fin=True)))
    return pkts


def test_incarnation_split(tmp):
    """Same 4-tuple, two incarnations, distinct ISNs, both complete."""
    pcap = os.path.join(tmp, "incarnations.pcap")
    pkts = (_session_packets(100.0, 1000, 5000, gen=1)
            + _session_packets(200.0, 9000, 7000, gen=2))
    _write_pcap(pcap, pkts)
    sessions, frames, entries, comp = wd.decode_node_pcap(pcap, CLIENT)
    ids = sorted(s["session_id"] for s in sessions)
    assert len(sessions) == 2, f"expected 2 incarnations, got {len(sessions)}"
    assert "#1000" in ids[0] and "#9000" in ids[1], ids
    assert all(s["conn_complete"] for s in sessions), sessions
    gens = sorted(e["gen"] for e in entries if e["frame_id"].endswith("request"))
    assert gens == [1, 2], f"payloads crossed incarnations: {gens}"
    assert comp["decode_errors"] == 0 and comp["reassembly_gaps"] == 0
    print("  incarnation split: 2 connections, own ISNs, own payloads  OK")


def test_frameless_reaches_gate(tmp):
    """A pre-frame failure must be judged: unattributed mid-run,
    tolerated at teardown."""
    import json
    pcap = os.path.join(tmp, "frameless.pcap")
    pkts = (_session_packets(100.0, 1000, 5000, gen=1)
            + _session_packets(150.0, 9000, 7000, gen=2, complete=False))
    _write_pcap(pcap, pkts)

    def fake_record(stop_t):
        rec = {"capture": {"capture_ok": True,
                           "nodes": {"n": {"pcap": pcap,
                                           "pcap_sha256":
                                           wd._sha256_file(pcap)}}},
               "node_ips": {"n": CLIENT},
               "author": "n", "t0_unix": 100.0, "window_s": 60,
               "configured_insert_ttl": 30,
               "daemons_stopped_at_unix": stop_t,
               "cell": {"interval": 5}}
        p = os.path.join(tmp, f"rec-{int(stop_t)}.json")
        json.dump(rec, open(p, "w"))
        return p

    # Case A: failure at t=150, daemons stopped at 400 -> NOT teardown.
    _, out = wd.decode_record(fake_record(400.0))
    assert any("#9000" in s for s in out["sessions_unattributed_anomalies"]), \
        out["sessions_unattributed_anomalies"]
    assert out["completeness_ok"] is False and out["decode_valid"] is False
    # Case B: daemons stopped at 151 -> teardown-attributable.
    _, out = wd.decode_record(fake_record(151.0))
    assert not out["sessions_unattributed_anomalies"], \
        out["sessions_unattributed_anomalies"]
    assert out["connections_frameless"] == 1
    print("  frame-less failure: gated mid-run, tolerated at teardown  OK")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        test_incarnation_split(tmp)
        test_frameless_reaches_gate(tmp)
    print("wiredecode selftest OK")


if __name__ == "__main__":
    main()
