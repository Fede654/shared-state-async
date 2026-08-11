"""Wire codec for the shared-state protocol (spec §2–§5).

Byte-exact encoder/decoder for handshake and message frames, plus a
blocking TCP client able to run a full sync session against a real
shared-state-async binary. This is the fixture-capture tool of spec §10:
until fixtures are captured, the payload JSON layout in payload_encode()
is the spec's ⚠️UNVERIFIED best guess and must not be trusted for
interop — the frame/handshake layer, in contrast, is directly read from
the C++ send/recv code and is expected to be exact.

Self-contained: python3 stdlib only.
  python3 wire.py selftest
  python3 wire.py sync <host> <type> [slice.json]   # against a live daemon
"""

import json
import socket
import struct
import sys

WIRE_PROTO_VERSION = 1
TCP_PORT = 3490
DATA_TYPE_NAME_MAX = 128
DATA_MAX = 1024 * 1024


# -- framing (spec §4) -----------------------------------------------------

def encode_frame(type_name: str, data: bytes) -> bytes:
    name = type_name.encode()
    if not 1 <= len(name) <= DATA_TYPE_NAME_MAX:
        raise ValueError("type name length")
    if not 2 <= len(data) <= DATA_MAX:
        raise ValueError("data length")
    return bytes([len(name)]) + name + struct.pack("!I", len(data)) + data


def decode_frame(buf: bytes):
    """Returns (type_name, data, consumed). Raises on malformed."""
    if len(buf) < 1:
        raise ValueError("short")
    nlen = buf[0]
    if not 1 <= nlen <= DATA_TYPE_NAME_MAX:
        raise ValueError(f"bad name length {nlen}")
    if len(buf) < 1 + nlen + 4:
        raise ValueError("short")
    name = buf[1:1 + nlen].decode()
    (dlen,) = struct.unpack("!I", buf[1 + nlen:1 + nlen + 4])
    if not 2 <= dlen <= DATA_MAX:
        raise ValueError(f"bad data length {dlen}")
    end = 1 + nlen + 4 + dlen
    if len(buf) < end:
        raise ValueError("short")
    return name, buf[1 + nlen + 4:end], end


def ack_for(frame: bytes) -> bytes:
    return struct.pack("!I", len(frame))


# -- payload (spec §6.7 — ⚠️UNVERIFIED layout) -----------------------------

def _xint64(n: int) -> dict:
    return {"xint64": n, "xstr64": str(n)}


def _unxint(v, default=0):
    """Integers cross the wire as {"xint64": n, "xstr64": "n"} where
    xint64 is authoritative (spec §6.7, fixture-verified)."""
    if isinstance(v, dict):
        return v.get("xint64", default)
    return default if v is None else v


def payload_encode(slice_: dict) -> bytes:
    """slice_: key -> {author, ttl, data[, version]}

    `mVersion` exists only on branches carrying the version-counter merge
    and uses the same xint64 convention as every other integer — verified
    against a running `merge_with_version` build. Sending it as a bare
    number makes the field silently deserialize as 0, which looks exactly
    like a merge bug in the binary under test. Nodes without the feature
    ignore the key."""
    arr = []
    for k, e in slice_.items():
        value = {"mAuthor": e["author"], "mTtl": _xint64(e["ttl"]),
                 "mData": e["data"]}
        if "version" in e:
            value["mVersion"] = _xint64(e["version"])
        arr.append({"key": k, "value": value})
    return json.dumps({"stateSlice": arr}, separators=(",", ":")).encode()


def payload_decode(data: bytes) -> dict:
    doc = json.loads(data.decode())
    out = {}
    for item in doc.get("stateSlice", []):
        v = item["value"]
        out[item["key"]] = {
            "author": v["mAuthor"],
            "ttl": _unxint(v.get("mTtl")),
            "data": v.get("mData"),
            "version": _unxint(v.get("mVersion")),
        }
    return out


# -- session (spec §3 + §5), blocking, for fixture capture -----------------

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def client_session(host: str, type_name: str, slice_: dict,
                   port: int = TCP_PORT, capture=None, timeout: float = 30):
    """Run one full client sync session. Returns the peer's slice.
    capture: optional dict populated with raw bytes for fixture storage."""
    ver = struct.pack("!I", WIRE_PROTO_VERSION)
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(ver)                          # §3 msg 1
        their = _recv_exact(s, 4)               # §3 msg 2
        if their != ver:
            raise ValueError(f"version mismatch: {their.hex()}")
        s.sendall(ver)                          # §3 msg 3 (RTT echo)

        frame = encode_frame(type_name, payload_encode(slice_))
        s.sendall(frame)                        # §5 request
        ack = _recv_exact(s, 4)
        if struct.unpack("!I", ack)[0] != len(frame):
            raise ValueError("ack mismatch on our frame")

        hdr = _recv_exact(s, 1)                 # §5 response
        nlen = hdr[0]
        rest = _recv_exact(s, nlen + 4)
        (dlen,) = struct.unpack("!I", rest[nlen:])
        data = _recv_exact(s, dlen)
        resp = hdr + rest + data
        s.sendall(ack_for(resp))
        if capture is not None:
            capture.update({"handshake_sent": ver.hex(), "request": frame.hex(),
                            "response": resp.hex()})
        name, payload, _ = decode_frame(resp)
        return name, payload_decode(payload)


# -- selftest --------------------------------------------------------------

def selftest():
    slice_ = {"LiMe-abc123": {"author": "LiMe-abc123", "ttl": 2401,
                              "data": {"hostname": "LiMe-abc123"}}}
    payload = payload_encode(slice_)
    frame = encode_frame("wifi_links_info", payload)
    name, data, consumed = decode_frame(frame)
    assert name == "wifi_links_info" and consumed == len(frame)
    assert payload_decode(data) == {"LiMe-abc123": {
        "author": "LiMe-abc123", "ttl": 2401,
        "data": {"hostname": "LiMe-abc123"}, "version": 0}}
    assert ack_for(frame) == struct.pack("!I", len(frame))
    for bad in (b"", bytes([0]) + b"x", bytes([200]) + b"x" * 300):
        try:
            decode_frame(bad + b"\x00" * 8)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted malformed frame {bad!r}")
    print("wire selftest OK")
    return True


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "selftest":
        selftest()
    elif len(sys.argv) >= 4 and sys.argv[1] == "sync":
        sl = {}
        if len(sys.argv) > 4:
            with open(sys.argv[4]) as f:
                sl = json.load(f)
        cap = {}
        name, resp = client_session(sys.argv[2], sys.argv[3], sl, capture=cap)
        print(json.dumps({"type": name, "state": resp, "capture": cap}, indent=1))
    else:
        print(__doc__)
