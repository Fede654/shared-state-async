"""Golden-fixture capture tool (spec §10).

Phase A — capture authentic CLIENT bytes:
    python3 capture.py server
  binds 0.0.0.0:3490 pretending to be the local daemon; then in another
  shell run the real binary, e.g.
    echo '{"k1": {...}}' | shared-state-async insert <type>
  The server performs the handshake, receives the frame, acks, echoes
  the same frame back as its "response", and writes everything under
  fixtures/captured/.

Phase B — capture authentic SERVER bytes:
    python3 capture.py client <host> <type> [request-payload.json]
  runs a wire.py client session against a REAL daemon and stores the
  raw request/response.

All captures land in fixtures/captured/<name>.{hex,json} — hex files
are the byte-exact golden fixtures; json files are decoded conveniences.
"""

import json
import os
import socket
import struct
import sys

import wire

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "fixtures", "captured")


def _save(name: str, raw: bytes, decoded=None):
    os.makedirs(OUTDIR, exist_ok=True)
    with open(os.path.join(OUTDIR, name + ".hex"), "w") as f:
        f.write(raw.hex() + "\n")
    if decoded is not None:
        with open(os.path.join(OUTDIR, name + ".json"), "w") as f:
            json.dump(decoded, f, indent=1, sort_keys=True)
            f.write("\n")
    print(f"captured {name}: {len(raw)} bytes")


def _recv_exact(c, n):
    buf = b""
    while len(buf) < n:
        chunk = c.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"peer closed at {len(buf)}/{n}")
        buf += chunk
    return buf


def fake_server(port=wire.TCP_PORT, once=False):
    ver = struct.pack("!I", wire.WIRE_PROTO_VERSION)
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.bind(("::", port))
    s.listen(4)
    print(f"fake daemon listening on :{port} — now run the real CLI "
          f"(e.g. insert/sync) against localhost")
    n = 0
    while True:
        c, addr = s.accept()
        n += 1
        print(f"connection {n} from {addr[0]}")
        try:
            hs1 = _recv_exact(c, 4)          # client version
            c.sendall(ver)                    # ours
            hs3 = _recv_exact(c, 4)          # RTT echo
            _save(f"client{n}_handshake", hs1 + hs3,
                  {"msg1": hs1.hex(), "msg3": hs3.hex(),
                   "version": struct.unpack("!I", hs1)[0]})

            hdr = _recv_exact(c, 1)
            nlen = hdr[0]
            rest = _recv_exact(c, nlen + 4)
            (dlen,) = struct.unpack("!I", rest[nlen:])
            data = _recv_exact(c, dlen)
            frame = hdr + rest + data
            type_name = rest[:nlen].decode()
            try:
                payload = json.loads(data.decode())
            except Exception as e:  # still save raw on parse surprise
                payload = {"UNPARSED": str(e)}
            _save(f"client{n}_request_{type_name}", frame, payload)

            c.sendall(wire.ack_for(frame))
            # respond by echoing the client's own frame (valid state msg)
            c.sendall(frame)
            ack = _recv_exact(c, 4)
            print(f"  client acked our response: {struct.unpack('!I', ack)[0]}")
        except Exception as e:
            print(f"  session error: {e}")
        finally:
            c.close()
        if once:
            break


def real_client(host, type_name, payload_file=None):
    slice_ = {}
    if payload_file:
        with open(payload_file) as f:
            slice_ = json.load(f)
    cap = {}
    name, resp = wire.client_session(host, type_name, slice_, capture=cap)
    _save(f"server_response_{name}", bytes.fromhex(cap["response"]),
          json.loads(bytes.fromhex(cap["response"])[
              1 + len(name.encode()) + 4:].decode()))
    _save(f"our_request_{name}", bytes.fromhex(cap["request"]))
    print(json.dumps({"type": name, "decoded_state": resp}, indent=1))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "server":
        fake_server(once="--once" in sys.argv)
    elif len(sys.argv) >= 4 and sys.argv[1] == "client":
        real_client(sys.argv[2], sys.argv[3],
                    sys.argv[4] if len(sys.argv) > 4 else None)
    else:
        print(__doc__)
