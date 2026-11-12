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
import math
import mimetypes
import os
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 8 * 1024 * 1024
LOG_CAP = 3000      # same cap as the browser
COOKIE = "recurser_token"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def _num(value, default=0):
    """A finite number, or the default (NaN, infinities, booleans and other types never get stored)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return value


def _flag(value) -> dict:
    """{"v": "suspended" | None, "t": ms}.  A cleared flag stays as a tombstone so the clearing travels."""
    if isinstance(value, dict):
        v = value.get("v")
        return {"v": v if isinstance(v, str) and v else None, "t": _num(value.get("t"))}
    return {"v": value if isinstance(value, str) and value else None, "t": 0}


def _gap(value) -> dict:
    """{"text": "...", "t": ms}.  An empty text is the tombstone of a cleared note."""
    if isinstance(value, dict):
        return {"text": str(value.get("text") or "")[:400], "t": _num(value.get("t"))}
    return {"text": str(value or "")[:400], "t": 0}


def clean_progress(data) -> dict:
    """Coerce anything into a well-formed history: unknown shapes are dropped, numbers are finite."""
    data = data if isinstance(data, dict) else {}

    def table(key):
        value = data.get(key)
        return value if isinstance(value, dict) else {}

    cards = {}
    for cid, state in table("cards").items():
        if isinstance(cid, str) and isinstance(state, dict):
            cards[cid] = {k: _num(v) for k, v in state.items() if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
                          and math.isfinite(v)}
    log = []
    for entry in data.get("log") if isinstance(data.get("log"), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
            item = {"id": entry["id"], "t": _num(entry.get("t")), "r": _num(entry.get("r"))}
            for extra in ("h", "c"):
                if entry.get(extra):
                    item[extra] = 1
            log.append(item)
    days = {}
    for key, val in table("days").items():
        if isinstance(key, str) and isinstance(val, dict):
            days[key] = {"n": int(_num(val.get("n"))), "ok": int(_num(val.get("ok"))), "fresh": int(_num(val.get("fresh"))), "t": _num(val.get("t"))}
    prefs = {k: v for k, v in table("prefs").items()
             if isinstance(k, str) and (isinstance(v, (str, bool)) or (isinstance(v, (int, float)) and math.isfinite(v)))}
    return {
        "version": 1,
        "updated": _num(data.get("updated")),
        "resetAt": _num(data.get("resetAt")),
        "restoredAt": _num(data.get("restoredAt")),
        "cards": cards,
        "log": log,
        "days": days,
        "touch": {k: _num(v) for k, v in table("touch").items() if isinstance(k, str)},
        "flags": {k: _flag(v) for k, v in table("flags").items() if isinstance(k, str)},
        "gaps": {k: _gap(v) for k, v in table("gaps").items() if isinstance(k, str)},
        "prefs": prefs,
    }


def merge_progress(a: dict | None, b: dict | None) -> dict:
    """Merge two review histories.  The same rules run in the browser (app/js/store.js) and both are
    checked against tests/srs_vectors.json:

    * cards: per card the most recent review (``last``) wins;
    * log: union by (id, t), oldest first, the last LOG_CAP entries;
    * flags and gap notes: the most recent change wins, and a cleared one is kept as a tombstone;
    * day counters: the larger figure; touch: the later time; prefs: the more recent file wins;
    * ``resetAt``: the later one, and everything dated at or before it is dropped ("Erase history"),
      unless a saved history was merged back in after it (``restoredAt``).
    """
    a, b = clean_progress(a), clean_progress(b)
    reset = max(a["resetAt"], b["resetAt"])
    restored = max(a["restoredAt"], b["restoredAt"])
    wall = reset if reset > restored else 0
    newer, older = (a, b) if a["updated"] >= b["updated"] else (b, a)

    def alive(t) -> bool:
        return not (wall > 0 and t <= wall)

    cards: dict = {}
    flags: dict = {}
    gaps: dict = {}
    touch: dict = {}
    days: dict = {}
    for src in (older, newer):
        for cid, state in src["cards"].items():
            cur = cards.get(cid)
            if cur is None or state.get("last", 0) >= cur.get("last", 0):
                cards[cid] = state
        for cid, flag in src["flags"].items():
            if cid not in flags or flag["t"] >= flags[cid]["t"]:
                flags[cid] = flag
        for cid, gap in src["gaps"].items():
            if cid not in gaps or gap["t"] >= gaps[cid]["t"]:
                gaps[cid] = gap
        for slug, t in src["touch"].items():
            touch[slug] = max(t, touch.get(slug, 0))
        for key, val in src["days"].items():
            cur = days.get(key, {"n": 0, "ok": 0, "fresh": 0, "t": 0})
            days[key] = {f: max(cur[f], val[f]) for f in ("n", "ok", "fresh", "t")}
    seen, log = set(), []
    for entry in older["log"] + newer["log"]:
        key = (entry["id"], entry["t"])
        if key not in seen and alive(entry["t"]):
            seen.add(key)
            log.append(entry)
    log.sort(key=lambda e: (e["t"], e["id"]))
    return {
        "version": 1,
        "updated": max(a["updated"], b["updated"]),
        "resetAt": reset,
        "restoredAt": restored,
        "cards": {cid: st for cid, st in cards.items() if alive(st.get("last", 0))},
        "log": log[-LOG_CAP:],
        "days": {k: v for k, v in days.items() if alive(v["t"])},
        "touch": {k: v for k, v in touch.items() if alive(v)},
        "flags": {k: v for k, v in flags.items() if alive(v["t"])},
        "gaps": {k: v for k, v in gaps.items() if alive(v["t"])},
        "prefs": {**older["prefs"], **newer["prefs"]},
    }


class ProgressFile:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()

    def read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return clean_progress(json.load(fh))
        except (OSError, ValueError):
            return clean_progress(None)

    def merge_in(self, incoming: dict) -> dict:
        with self.lock:
            merged = merge_progress(self.read(), incoming)
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, separators=(",", ":"), allow_nan=False)
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
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")   # no other site may embed the deck
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

        def _foreign(self) -> bool:
            """A request another web page caused, or one that reached us under a foreign name (DNS rebinding)."""
            # Opening the page from a link elsewhere is fine; being embedded or fetched by another page is not.
            if (self.headers.get("Sec-Fetch-Site", "none").lower() in ("cross-site", "same-site")
                    and self.headers.get("Sec-Fetch-Mode", "").lower() != "navigate"):
                return True
            if token is None:   # loopback mode: the page is only ever opened under a loopback name
                port = self.server.server_address[1]
                names = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")} | ({"127.0.0.1", "localhost", "[::1]"} if port == 80 else set())
                if self.headers.get("Host", "") not in names:
                    return True
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc not in names:
                    return True
            return False

        # -- routes ------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            if self._foreign():
                return self._send(HTTPStatus.FORBIDDEN, b"This page only answers its own tab.", "text/plain; charset=utf-8")
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
            if self._foreign():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin write refused"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "body must be 1 byte to 8 MiB of JSON"})
            def refuse(name):
                raise ValueError(name)

            try:
                incoming = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=refuse)
            except (UnicodeDecodeError, ValueError):
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
