"""A protocol-speaking peer that counts what a daemon does to it.

Some behaviour is only visible from the far side of a sync: how often a
node actually publishes, whether it retries, what it sends. The daemon
does not log any of that at production debug levels, so the harness
provides a peer of its own — a real listener speaking the real wire
format, recording every session with a timestamp.

Runs in the harness's own namespace (reachable from every node at the
bridge address), so it needs no extra network setup.
"""

import socket
import struct
import threading
import time

import wire


class FakePeer:
    """Listens on TCP 3490 and records each sync session it serves."""

    def __init__(self, bind="10.99.0.1", port=wire.TCP_PORT, state=None):
        self.bind = bind
        self.port = port
        self.state = state or {}
        self.sessions = []          # (timestamp, type_name, n_entries)
        self._stop = threading.Event()
        self._sock = None
        self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def start(self):
        self._sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        self._sock.bind(("::", self.port))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._sock:
            self._sock.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._session(conn)
            except Exception:
                pass
            finally:
                conn.close()

    def _session(self, conn):
        conn.settimeout(10)
        ver = struct.pack("!I", wire.WIRE_PROTO_VERSION)
        if self._recv(conn, 4) != ver:
            return
        conn.sendall(ver)
        self._recv(conn, 4)                     # RTT echo

        hdr = self._recv(conn, 1)
        nlen = hdr[0]
        rest = self._recv(conn, nlen + 4)
        type_name = rest[:nlen].decode()
        (dlen,) = struct.unpack("!I", rest[nlen:])
        data = self._recv(conn, dlen)
        conn.sendall(struct.pack("!I", 1 + nlen + 4 + dlen))

        try:
            entries = len(wire.payload_decode(data))
        except Exception:
            entries = -1
        self.sessions.append((time.time(), type_name, entries))

        reply = wire.encode_frame(type_name, wire.payload_encode(self.state))
        conn.sendall(reply)
        self._recv(conn, 4)                     # their ack

    @staticmethod
    def _recv(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    # -- observations ---------------------------------------------------
    def count_since(self, t0):
        return len([s for s in self.sessions if s[0] >= t0])

    def intervals(self):
        ts = [s[0] for s in self.sessions]
        return [round(b - a, 1) for a, b in zip(ts, ts[1:])]
