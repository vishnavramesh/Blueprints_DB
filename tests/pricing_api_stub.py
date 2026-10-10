"""A local stand-in for the B4P Pricing Enrichment API (f25_b4p_tech_project).

Implements the part of its contract `b4p prices --enrich-url` uses:
  POST /v1/enrich/batch   Authorization: Bearer <key>
  body  {"items": [ItemPayload...], "options": {...}}
  reply {"run_id": str, "items": [EnrichedItem...], "usage": {...}}   (same order)
  401 without a valid key, 413 above MAX_BATCH_SIZE.
Prices come from a fixed {lower-case name: unit price} table; anything else
is answered like a cache miss with no provider: status 'needs_review'.
No network, no LLMs.

Run by hand:  python tests/pricing_api_stub.py --port 8765 --key test-key
"""
from __future__ import annotations

import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_server(prices: dict[str, float], key: str = "test-key", port: int = 0,
                max_batch_size: int = 100, review: set[str] = frozenset()):
    """Returns a ThreadingHTTPServer; .requests collects every request body."""
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/v1/enrich/batch":
                return self._reply(404, {"detail": "Not Found"})
            if self.headers.get("Authorization") != f"Bearer {key}":
                return self._reply(401, {"detail": "Invalid API key"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            requests.append(body)
            items = body["items"]
            if len(items) > max_batch_size:
                return self._reply(413, {"detail": f"Batch size {len(items)} exceeds MAX_BATCH_SIZE"})
            out = []
            for it in items:
                name = it["name"].lower()
                price = prices.get(name)
                hit = price is not None
                out.append({
                    "external_id": it.get("external_id"),
                    "status": "priced" if hit else "needs_review",
                    "source": "cache" if hit else "needs_review",
                    "unit_price": price,
                    "total_price": price * max(it.get("quantity", 1), 1) if hit else None,
                    "currency": "USD", "price_url": "https://example.test/p" if hit else "",
                    "provider": "", "matched_item": it["name"] if hit else "",
                    "confidence": 0.9 if hit else None,
                    "needs_review": (not hit) or name in review,
                    "review_warning": "price outside bounds" if name in review else "",
                    "input_tokens": 0, "output_tokens": 0, "provider_calls": 0, "cost_usd": 0.0,
                })
            self._reply(200, {"run_id": str(uuid.uuid4()), "items": out,
                              "usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                        "total_tokens": 0, "tool_calls": 0, "cost_usd": 0.0}})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.requests = requests
    return server


def start(prices, **kw):
    """Start in a background thread; returns (server, base_url)."""
    server = make_server(prices, **kw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--key", default="test-key")
    ap.add_argument("--prices", help="JSON file {name: unit price}", default=None)
    a = ap.parse_args()
    table = json.load(open(a.prices)) if a.prices else {}
    print(f"stub pricing API on http://127.0.0.1:{a.port} ({len(table)} prices)", flush=True)
    make_server({k.lower(): v for k, v in table.items()}, key=a.key, port=a.port).serve_forever()
