"""Test harness: throwaway Postgres databases, a fake dashboard feed, and a
b4p-service process pointed at both.

Nothing here touches the real `b4p` database. Every test gets a fresh copy of
`b4p_test_template` (schema.sql + migrations/*.sql, built once per run).

Environment:
    B4P_BIN     path to the b4p-service binary (default: ../build/b4p-service)
    B4P_REPO    repo root (default: two levels up)
    PGHOST etc. standard libpq variables, if Postgres isn't on the local socket
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg

REPO = Path(os.environ.get("B4P_REPO", Path(__file__).resolve().parents[2]))
BIN = os.environ.get("B4P_BIN", str(REPO / "b4p-service/build/b4p-service"))
TEMPLATE = "b4p_test_template"
TOKEN = "test-token"

_template_built = False


def _psql(db, *args):
    subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", db, *args],
                   check=True, capture_output=True)


def _drop(db):
    subprocess.run(["dropdb", "--if-exists", "--force", db], check=True, capture_output=True)


def build_template():
    """schema.sql + every migration, in order, into b4p_test_template."""
    global _template_built
    if _template_built:
        return
    _drop(TEMPLATE)
    subprocess.run(["createdb", TEMPLATE], check=True)
    _psql(TEMPLATE, "-f", str(REPO / "schema.sql"))
    for m in sorted((REPO / "migrations").glob("*.sql")):
        _psql(TEMPLATE, "-f", str(m))
    _template_built = True


def fresh_db(name):
    build_template()
    _drop(name)
    subprocess.run(["createdb", "-T", TEMPLATE, name], check=True)
    return name


def connect(db):
    return psycopg.connect(dbname=db, autocommit=True)


# ---------------------------------------------------------------------------
# Fake dashboard backend
# ---------------------------------------------------------------------------

class FakeFeed:
    """Serves /api/supplies and /requests from mutable Python lists.
    Set `.status[path] = 500` or `.raw[path] = b"garbage"` to simulate a
    broken backend."""

    def __init__(self):
        self.supplies, self.requests = [], []
        self.status, self.raw = {}, {}
        feed = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                body = feed.raw.get(path)
                if body is None:
                    data = {"/api/supplies": feed.supplies, "/requests": feed.requests}.get(path)
                    if data is None:
                        self.send_error(404)
                        return
                    body = json.dumps(data).encode()
                self.send_response(feed.status.get(path, 200))
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def supply_row(n, **over):
    """A feed row shaped like the real sheet export."""
    row = {
        "Days in Warehouse": "0", "Manufacturer Name": "Medline Industries, LP",
        "Category": "Wound Care & Bandages", "General": "gauze",
        "Name": f"Test Gauze Pad {n}x{n} in", "Lot Number": f"LOT{n}",
        "Notes": "", "Quantity": "10", "Date of Expiration": "12/31/2030",
        "Pallet Number": "1", "Box Number": f"WOUND {n}",
        "Image URL": f"https://drive.google.com/open?id=test{n}",
        "Review": "No review needed", "Unnamed_13": "", "Unnamed_14": "",
        "AI Generated": "", "From Form": "", "_sheet_row": str(n + 1),
    }
    row.update(over)
    return row


def request_row(rid, item, qty="1", status="Under Review", **over):
    row = {"Request ID": rid, "Org Name": "Test Clinic", "Org Email": "clinic@example.org",
           "Item Name": item, "Category": "Wound Care & Bandages",
           "Quantity Requested": qty, "Status": status,
           "Timestamp": "2026-05-23T05:23:06.638914+00:00", "Review Flag": "FALSE"}
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# The service under test
# ---------------------------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Service:
    def __init__(self, db, feed_url, token=TOKEN, pool=4, threads=4):
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = token
        env = dict(os.environ, B4P_DB=f"dbname={db}", B4P_PORT=str(self.port),
                   B4P_HOST="127.0.0.1", B4P_FEED_HOST=feed_url, B4P_SYNC_EVERY="0",
                   B4P_API_TOKEN=token or "", B4P_DB_POOL=str(pool),
                   B4P_THREADS=str(threads))
        self.log_path = Path(os.environ.get("TMPDIR", "/tmp")) / f"b4p-test-{self.port}.log"
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen([BIN], env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 15
        while True:
            try:
                if self.get("/health")[0] == 200:
                    break
            except OSError:
                pass
            if self.proc.poll() is not None or time.time() > deadline:
                raise RuntimeError("service did not start:\n" + self.log_path.read_text())
            time.sleep(0.05)
        self.wait_idle()  # the startup cycle

    def request(self, method, path, body=None, token=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token and self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            with e:
                raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode(errors="replace")

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None, token=True):
        return self.request("POST", path, body if body is not None else {}, token)

    def last_run(self):
        return self.get("/sync/status")[1]

    def wait_idle(self, after_run=0, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.last_run()
            run = st["last_run"]
            if not st["running"] and run and run["run_id"] > after_run and run["finished_at"]:
                return run
            time.sleep(0.05)
        raise TimeoutError("sync did not finish:\n" + self.log_path.read_text()[-3000:])

    def sync(self):
        """Run one cycle and return its sync_run row."""
        before = (self.last_run()["last_run"] or {}).get("run_id", 0)
        code, _ = self.post("/sync/run")
        assert code == 202, code
        return self.wait_idle(after_run=before)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.log.close()
        if self.proc.returncode == 0:
            self.log_path.unlink(missing_ok=True)
        return self.proc.returncode
