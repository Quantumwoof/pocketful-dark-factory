"""HTTP adapter. The harness speaks only HTTP; nothing here is imported by it."""
from __future__ import annotations

import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from engine import LEDGER, STAGE, ApiError

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PAGES = {"/", "/requests", "/split", "/signup", "/login", "/authorizations"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send(self, status: int, payload, *, content_type: str | None = None) -> None:
        if payload is None:
            body = b""
        elif isinstance(payload, (bytes, bytearray)):
            body = bytes(payload)
        else:
            body = json.dumps(payload, ensure_ascii=False).encode()
            content_type = content_type or "application/json; charset=utf-8"
        self.send_response(status)
        if content_type and body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _fail(self, status: int, code: str, message: str | None = None) -> None:
        self._send(status, {"error": {"code": code, "message": message or code.replace("_", " ")}})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise ApiError(400, "malformed_request", "body is not valid JSON")
        if not isinstance(parsed, dict):
            raise ApiError(400, "malformed_request", "body must be a JSON object")
        return parsed

    def _query(self) -> dict:
        parsed = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _wants_html(self) -> bool:
        return "text/html" in (self.headers.get("Accept") or "").lower()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str) -> None:
        path = unquote(urlparse(self.path).path)
        if len(path) > 1:
            path = path.rstrip("/")
        try:
            if method == "GET" and path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if method == "GET" and path == "/health":
                return self._send(200, {"status": "ok"})
            if method == "POST" and path == "/_test/reset":
                body = self._read_json()
                with LEDGER.lock:
                    LEDGER.reset(body)
                return self._send(204, None)
            if method == "GET" and path == "/_test/export":
                with LEDGER.lock:
                    payload = LEDGER.export()
                return self._send(200, payload)
            if method == "POST" and path == "/_test/import":
                body = self._read_json()
                with LEDGER.lock:
                    LEDGER.import_state(body)
                return self._send(204, None)
            if method == "POST" and path == "/auth/signup":
                body = self._read_json()
                with LEDGER.lock:
                    status, payload = LEDGER.signup(body)
                return self._send(status, payload)
            if method == "POST" and path == "/auth/login":
                body = self._read_json()
                with LEDGER.lock:
                    status, payload = LEDGER.login(body)
                return self._send(status, payload)
            if STAGE >= 2 and method == "GET" and path in PAGES and (path not in ("/requests", "/authorizations") or self._wants_html()):
                return self._page()
            body = self._read_json() if method == "POST" else None
            with LEDGER.lock:
                LEDGER._expire()
                user = LEDGER._bearer(self.headers.get("Authorization"))
                status, payload = self._dispatch(method, path, user, body)
            return self._send(status, payload)
        except ApiError as exc:
            return self._fail(exc.status, exc.code, exc.message)
        except Exception:
            traceback.print_exc()
            return self._fail(500, "internal", "internal error")

    def _dispatch(self, method: str, path: str, user, body):
        header = self.headers.get("Idempotency-Key")
        query = self._query()
        if method == "GET" and path == "/me":
            return LEDGER.me(user, query)
        if method == "POST" and path == "/payments":
            return LEDGER.create_payment(user, body, header)
        if method == "POST" and path == "/requests":
            return LEDGER.create_request(user, body, header)
        match = re.fullmatch(r"/requests/([^/]+)/pay", path)
        if method == "POST" and match:
            return LEDGER.pay_request(user, match.group(1), body, header)
        match = re.fullmatch(r"/requests/([^/]+)/decline", path)
        if method == "POST" and match:
            return LEDGER.decline_request(user, match.group(1))
        match = re.fullmatch(r"/requests/([^/]+)/cancel", path)
        if method == "POST" and match:
            return LEDGER.cancel_request(user, match.group(1))
        if method == "GET" and path == "/requests":
            return LEDGER.list_requests(user, query)
        if method == "POST" and path == "/splits":
            return LEDGER.create_split(user, body, header)
        if method == "GET" and path == "/activity":
            return LEDGER.activity(user, query)
        if method == "POST" and path == "/settlements":
            return LEDGER.create_settlement(user, body, header)
        if method == "POST" and path == "/authorizations":
            return LEDGER.create_authorization(user, body, header)
        match = re.fullmatch(r"/authorizations/([^/]+)/capture", path)
        if method == "POST" and match:
            return LEDGER.capture_authorization(user, match.group(1), body, header)
        match = re.fullmatch(r"/authorizations/([^/]+)/void", path)
        if method == "POST" and match:
            return LEDGER.void_authorization(user, match.group(1))
        if method == "GET" and path == "/authorizations":
            return LEDGER.list_authorizations(user, query)
        if method == "GET" and path == "/statement":
            return LEDGER.statement(user, query)
        match = re.fullmatch(r"/payments/([^/]+)/corrections", path)
        if method == "POST" and match:
            return LEDGER.correct_payment(user, match.group(1), body, header)
        match = re.fullmatch(r"/payments/([^/]+)/revisions", path)
        if method == "GET" and match:
            return LEDGER.list_revisions(user, match.group(1))
        match = re.fullmatch(r"/payments/([^/]+)/refunds", path)
        if method == "POST" and match:
            return LEDGER.refund_payment(user, match.group(1), body, header)
        if method == "POST" and path == "/correction-batches":
            return LEDGER.correction_batch(user, body, header)
        raise ApiError(404, "not_found", "unknown route")

    def _page(self) -> None:
        html = os.path.join(STATIC, "index.html")
        with open(html, "rb") as handle:
            self._send(200, handle.read(), content_type="text/html; charset=utf-8")

    def _static(self, name: str) -> None:
        if ".." in name or name not in ("app.js", "app.css"):
            return self._fail(404, "not_found", "unknown asset")
        kind = "text/css; charset=utf-8" if name.endswith(".css") else "text/javascript; charset=utf-8"
        with open(os.path.join(STATIC, name), "rb") as handle:
            self._send(200, handle.read(), content_type=kind)


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    request_queue_size = 256
    daemon_threads = True


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    print(f"pocketful stage {STAGE} listening on 0.0.0.0:{port}", flush=True)
    Server(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
