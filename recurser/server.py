"""Localhost server — static app plus a tiny progress API.  Stdlib only.

    python -m recurser serve                 # http://127.0.0.1:8765, this machine only
    python -m recurser serve --host 0.0.0.0  # reachable from your phone on the same network

The app works without any server (review history lives in the browser).  When
it is served from here it also saves that history through ``/api/progress``,
so every browser that opens this address shares one history file
(``~/.recurser/progress.json`` unless ``--progress`` says otherwise).

When the server listens beyond loopback it requires a token: open the printed
link once (``/?token=...``) and a cookie keeps that browser signed in.
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 8 * 1024 * 1024
LOG_CAP = 5000
COOKIE = "recurser_token"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def merge_progress(a: dict | None, b: dict | None) -> dict:
    """Merge two review histories: per card the most recent review wins."""
    a, b = a or {}, b or {}
    newer, older = (a, b) if a.get("updated", 0) >= b.get("updated", 0) else (b, a)
    cards = dict(older.get("cards") or {})
    for cid, state in (newer.get("cards") or {}).items():
        mine = cards.get(cid)
        if not mine or (state or {}).get("last", 0) >= (mine or {}).get("last", 0):
            cards[cid] = state
    for cid, state in (older.get("cards") or {}).items():  # keep the fresher one when the older file had it
        if (state or {}).get("last", 0) > (cards.get(cid) or {}).get("last", 0):
            cards[cid] = state
    seen, log = set(), []
    for entry in (older.get("log") or []) + (newer.get("log") or []):
        key = (entry.get("id"), entry.get("t"))
        if key not in seen and entry.get("id"):
            seen.add(key)
            log.append(entry)
    log.sort(key=lambda e: e.get("t", 0))
    touch = dict(older.get("touch") or {})
    for slug, t in (newer.get("touch") or {}).items():
        touch[slug] = max(t, touch.get(slug, 0))
    flags = dict(older.get("flags") or {})
    flags.update(newer.get("flags") or {})
    gaps = dict(older.get("gaps") or {})
    for cid, gap in (newer.get("gaps") or {}).items():
        if (gap or {}).get("t", 0) >= (gaps.get(cid) or {}).get("t", 0):
            gaps[cid] = gap
    days: dict = {}
    for src in (older, newer):  # per-day counters: keep the larger figure seen on either side
        for key, val in (src.get("days") or {}).items():
            cur = days.get(key, {})
            days[key] = {f: max(int(cur.get(f, 0)), int((val or {}).get(f, 0))) for f in ("n", "ok", "fresh")}
    return {
        "version": 1,
        "updated": max(a.get("updated", 0), b.get("updated", 0)),
        "cards": cards,
        "log": log[-LOG_CAP:],
        "days": days,
        "touch": touch,
        "flags": flags,
        "gaps": gaps,
        "prefs": {**(older.get("prefs") or {}), **(newer.get("prefs") or {})},
    }


class ProgressFile:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()

    def read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def merge_in(self, incoming: dict) -> dict:
        with self.lock:
            merged = merge_progress(self.read(), incoming)
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, separators=(",", ":"))
            os.replace(tmp, self.path)
            return merged


def make_handler(app_dir: str, progress: ProgressFile, token: str | None, info: dict):
    app_dir = os.path.realpath(app_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Recurser/1"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            return

        # -- helpers -----------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data):
            self._send(status, json.dumps(data, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

        def _authorised(self, query: dict) -> tuple[bool, dict]:
            if not token:
                return True, {}
            supplied = (query.get("token") or [""])[0]
            if supplied and hmac.compare_digest(supplied, token):
                return True, {"Set-Cookie": f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"}
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE and hmac.compare_digest(value, token):
                    return True, {}
            return False, {}

        # -- routes ------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            ok, extra = self._authorised(parse_qs(url.query))
            if not ok:
                return self._send(HTTPStatus.UNAUTHORIZED, b"Open the link printed by `recurser serve` (it carries the token).",
                                  "text/plain; charset=utf-8")
            if url.path == "/api/health":
                return self._json(200, {"ok": True, **info})
            if url.path == "/api/progress":
                return self._json(200, progress.read())
            rel = url.path.lstrip("/") or "index.html"
            full = os.path.realpath(os.path.join(app_dir, rel))
            if not (full == app_dir or full.startswith(app_dir + os.sep)) or not os.path.isfile(full):
                return self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as fh:
                self._send(200, fh.read(), ctype, extra)

        def do_PUT(self):
            url = urlsplit(self.path)
            ok, _ = self._authorised(parse_qs(url.query))
            if not ok:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "token required"})
            if url.path != "/api/progress":
                return self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            if token is None and self.headers.get("Origin"):
                # loopback mode: only this origin may write (defence against cross-site requests)
                host = self.headers.get("Host", "")
                if urlsplit(self.headers["Origin"]).netloc != host:
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin write refused"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "body must be 1 byte to 8 MiB of JSON"})
            try:
                incoming = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "body is not valid JSON"})
            if not isinstance(incoming, dict) or not isinstance(incoming.get("cards", {}), dict):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "expected a progress object with a 'cards' map"})
            return self._json(200, progress.merge_in(incoming))

        do_POST = do_PUT

    return Handler


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def create_server(app_dir: str, *, host: str = "127.0.0.1", port: int = 8765, progress_path: str | None = None,
                  token: str | None = None, info: dict | None = None) -> tuple[ThreadingHTTPServer, str | None]:
    """Build the server (not yet serving).  Returns (server, token-or-None)."""
    progress_path = progress_path or os.path.join(os.path.expanduser("~"), ".recurser", "progress.json")
    if not is_loopback(host) and not token:
        token = secrets.token_urlsafe(18)
    if is_loopback(host):
        token = token or None
    handler = make_handler(app_dir, ProgressFile(progress_path), token, info or {})
    return ThreadingHTTPServer((host, port), handler), token
