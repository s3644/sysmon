#!/usr/bin/env python3
"""Live resource monitor: runs sampler.py locally and over SSH, streams to a web page.

  python3 server.py                       # this machine only, http://127.0.0.1:8765
  python3 server.py --bind 0.0.0.0        # reachable from the LAN
  python3 server.py --host 'Workstation=local' --host 'GPU box=user@gpu-box.local'

Without --host, hosts are read from hosts.conf next to this file (one 'Label=target'
per line, '#' comments), falling back to the local machine alone.
"""
import argparse
import collections
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLER = os.path.join(HERE, "sampler.py")
HOSTS_CONF = os.path.join(HERE, "hosts.conf")
DEFAULT_HOSTS = ["This machine=local"]


class Host:
    def __init__(self, idx, label, target, interval, keep):
        self.id, self.label, self.target, self.interval = f"h{idx}", label, target, interval
        self.history = collections.deque(maxlen=keep)
        self.last = None
        self.status, self.error = "connecting", ""
        self.proc = None
        self.seen = 0.0  # monotonic time of the last sample

    def info(self):
        return {"id": self.id, "label": self.label, "target": self.target, "interval": self.interval,
                "status": self.status, "error": self.error}

    def cmd(self):
        args = ["-u", "-", "--interval", str(self.interval)]
        if self.target == "local":
            return ["python3"] + args
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                "--", self.target, "python3"] + args


def load_hosts(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        specs = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    bad = [l for l in specs if "=" not in l]
    if bad:
        sys.exit(f"{path}: expected 'Label=target', got: {bad[0]}")
    return specs or None


def slim(s):
    """The few numbers the history charts need, so the initial load stays small."""
    g = s["gpus"]
    return {
        "ts": s["ts"], "cpu": s["cpu"], "mem": s["mem"]["used"],
        "gpu": [x["util"] for x in g], "gmem": [x["mem_used"] for x in g], "gpow": [x["power"] for x in g],
        "rx": sum(v[0] for v in s["net_io"].values()), "tx": sum(v[1] for v in s["net_io"].values()),
        "rd": sum(v[0] for v in s["disk_io"].values()), "wr": sum(v[1] for v in s["disk_io"].values()),
        "tcpu": s["temp"]["cpu"],
    }


class Hub:
    MAX_SUBS = 64

    def __init__(self, hosts):
        self.hosts = hosts
        self.subs = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            if len(self.subs) >= self.MAX_SUBS:
                return None
            self.subs.add(q)
        return q

    def subscribed(self, q):
        with self.lock:
            return q in self.subs

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)

    def publish(self, event, data):
        msg = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
        with self.lock:
            for q in list(self.subs):
                try:
                    q.put_nowait(msg)
                except queue.Full:  # a stalled tab; drop it (stream() then closes) rather than buffer forever
                    self.subs.discard(q)

    def set_status(self, h, status, error=""):
        if (h.status, h.error) != (status, error):
            h.status, h.error = status, error
            self.publish("status", h.info())

    def run(self, h):
        with open(SAMPLER, "rb") as f:
            script = f.read()
        backoff = 2
        while True:
            self.set_status(h, "connecting")
            try:
                h.proc = subprocess.Popen(h.cmd(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, start_new_session=True)
                h.proc.stdin.write(script)
                h.proc.stdin.close()
                h.seen = time.monotonic()
                for line in h.proc.stdout:
                    try:
                        s = json.loads(line)
                        row = slim(s)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        continue  # e.g. a remote shell rc file printing to stdout
                    backoff = 2
                    h.seen = time.monotonic()
                    h.last = s
                    h.history.append(row)
                    self.set_status(h, "up")
                    self.publish("sample", {"id": h.id, "s": s})
                h.proc.wait()
                err = h.proc.stderr.read().decode(errors="replace").strip().splitlines()
                self.set_status(h, "down", err[-1] if err else f"exit {h.proc.returncode}")
            except Exception as e:  # keep the host thread alive whatever happens
                self.kill(h)
                self.set_status(h, "down", f"{type(e).__name__}: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def watchdog(self, stale):
        """Restart a sampler that has gone quiet (e.g. stuck on a hung mount or a dead link)."""
        while True:
            time.sleep(5)
            for h in self.hosts:
                if h.proc and h.proc.poll() is None and time.monotonic() - h.seen > stale:
                    self.set_status(h, "down", f"no data for {stale:.0f} s; restarting")
                    self.kill(h)

    @staticmethod
    def kill(h):
        if h.proc and h.proc.poll() is None:
            try:
                os.killpg(h.proc.pid, signal.SIGTERM)
            except OSError:
                pass

    def stop(self):
        for h in self.hosts:
            self.kill(h)


def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # drop idle or stalled connections instead of holding a thread forever

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self.send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                body = [dict(h.info(), history=list(h.history), last=h.last) for h in hub.hosts]
                self.send(200, json.dumps(body, separators=(",", ":")).encode(), "application/json")
            elif path == "/api/stream":
                self.stream()
            else:
                self.send(404, b"not found", "text/plain")

        def stream(self):
            q = hub.subscribe()
            if q is None:
                self.send(503, b"too many viewers", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                # Once publish() has dropped this queue, end the response so the browser reconnects
                # and reloads /api/state instead of sitting on a frozen page.
                while hub.subscribed(q):
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        msg = b": keepalive\n\n"
                    self.wfile.write(msg)
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", action="append",
                    help="'Label=local' or 'Label=user@host' (repeatable; overrides hosts.conf)")
    ap.add_argument("--hosts-file", default=HOSTS_CONF, help="default: hosts.conf next to server.py")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    ap.add_argument("--keep", type=int, default=60, help="minutes of history to keep")
    args = ap.parse_args()
    if args.interval <= 0 or args.keep <= 0:
        ap.error("--interval and --keep must be > 0")

    keep = max(1, int(args.keep * 60 / args.interval))
    specs = args.host or load_hosts(args.hosts_file) or DEFAULT_HOSTS
    if any("=" not in s for s in specs):
        ap.error("--host expects 'Label=target'")
    hosts = [Host(i, *spec.split("=", 1), args.interval, keep) for i, spec in enumerate(specs)]
    hub = Hub(hosts)
    for h in hosts:
        threading.Thread(target=hub.run, args=(h,), daemon=True).start()
    threading.Thread(target=hub.watchdog, args=(max(30.0, 5 * args.interval),), daemon=True).start()

    srv = ThreadingHTTPServer((args.bind, args.port), make_handler(hub))
    srv.daemon_threads = True

    def shutdown(*_):
        hub.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    print(f"sysmon on http://{args.bind}:{args.port}  hosts: {', '.join(h.label for h in hosts)}", flush=True)
    try:
        srv.serve_forever()
    finally:
        hub.stop()


if __name__ == "__main__":
    main()
