"""The web server: the app, each person's review history, and the assistant.  Stdlib only.

    python -m recurser serve                 # http://127.0.0.1:8765, this machine only
    python -m recurser serve --host 0.0.0.0  # reachable from your phone on the same network
    python -m recurser serve --users users.json --config config.yaml     # a team, e.g. in Kubernetes

Alone, the server keeps one history file (``~/.recurser/progress.json``) and, beyond loopback, asks
for one token.  With a users file it serves a team: each person signs in with a personal link
(``/?token=...``), sees only the projects listed for them, and has a history of their own.

    GET  /                    the app
    GET  /data/bundle.js      the decks this person may see
    GET  /api/health          who is signed in and what the server offers
    GET  /api/progress        this person's review history        PUT merges into it
    POST /api/ask             {question, slug?}: an answer from the decks, with the passages it rests on
    POST /api/critique        {id, answer}: a typed answer compared with the card's reference
    GET  /healthz             liveness, no sign-in
    GET  /openapi.json, /docs the API described
"""
from __future__ import annotations

import hmac
import json
import math
import mimetypes
import os
import re
import secrets
import threading
import time
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


# --------------------------------------------------------------------------
# Who may open the page, and what each person sees
# --------------------------------------------------------------------------
USER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


def load_users(path: str) -> list[dict]:
    """The users file: ``{"users": [{"name": "sam", "token": "...", "projects": "*" | ["slug", ...]}]}``."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    users = data.get("users") if isinstance(data, dict) else data
    if not isinstance(users, list) or not users:
        raise ValueError(f"{path}: expected a non-empty list under 'users'")
    seen_names, seen_tokens = set(), set()
    for user in users:
        if not isinstance(user, dict) or not USER_NAME.match(str(user.get("name", ""))):
            raise ValueError(f"{path}: every user needs a 'name' of lower-case letters, digits, '.', '_' or '-'")
        token, projects = user.get("token"), user.get("projects", [])
        if not isinstance(token, str) or len(token) < 20:
            raise ValueError(f"{path}: user {user['name']!r} needs a 'token' of at least 20 characters")
        if not (projects == "*" or (isinstance(projects, list) and all(isinstance(x, str) for x in projects))):
            raise ValueError(f"{path}: user {user['name']!r}: 'projects' is \"*\" or a list of project names")
        if user["name"] in seen_names or token in seen_tokens:
            raise ValueError(f"{path}: names and tokens must be unique ({user['name']!r})")
        seen_names.add(user["name"])
        seen_tokens.add(token)
    return users


class View:
    """What one audience sees: its edition, the bundle the app loads, the index questions are answered from."""

    def __init__(self, edition: dict):
        self.edition = edition
        self.cards = {c["id"]: c for c in edition["cards"]}
        self.projects = {p["slug"]: p for p in edition["projects"]}
        self.lock = threading.Lock()
        self._bundle: bytes | None = None
        self._index = None

    @property
    def bundle_js(self) -> bytes:
        with self.lock:
            if self._bundle is None:
                from . import bundle as bundle_mod

                self._bundle = bundle_mod.bundle_js(bundle_mod.build_bundle(self.edition)).encode("utf-8")
            return self._bundle

    @property
    def index(self):
        with self.lock:
            if self._index is None:
                from . import retrieval

                self._index = retrieval.Index(self.edition)
            return self._index


class Site:
    """Everything a request needs: who is asking, what they may see, where their history lives."""

    def __init__(self, edition: dict, *, progress_path: str | None = None, progress_dir: str | None = None, token: str | None = None,
                 users: list[dict] | None = None, user_header: str = "", settings: dict | None = None, client=None):
        from . import config as config_mod, store

        self.settings = settings or config_mod.merge(None)
        self.edition = edition
        self.token = token
        self.users = users or []
        self.team = bool(self.users)
        self.user_header = user_header.strip()
        self.progress_path = progress_path or os.path.join(os.path.expanduser("~"), ".recurser", "progress.json")
        self.progress_dir = progress_dir or os.path.dirname(self.progress_path)
        self.client = client                      # an Anthropic client, or None: then nothing is asked of the API
        self.lock = threading.Lock()
        self._views: dict[tuple, View] = {}
        self._files: dict[str, ProgressFile] = {}
        self._asked: dict[str, list[float]] = {}
        self.slots = threading.BoundedSemaphore(self.settings["server"]["max_concurrent_answers"])
        self._store = store
        for user in self.users:                   # a typo in a project list must stop the server, not hide a deck silently
            if user.get("projects") != "*":
                try:
                    user["_slugs"] = tuple(sorted(store.resolve_projects(edition, user.get("projects") or [])))
                except KeyError as exc:
                    raise ValueError(f"user {user['name']!r}: unknown project {exc.args[0]}") from None

    # -- identity ------------------------------------------------------
    def by_token(self, supplied: str) -> dict | None:
        found = None
        for user in self.users:                   # constant-time per user, no early exit
            if hmac.compare_digest(supplied.encode("utf-8"), user["token"].encode("utf-8")):
                found = user
        return found

    def by_name(self, name: str) -> dict | None:
        name = name.strip().lower()
        for user in self.users:
            if name and name in (user["name"], str(user.get("email", "")).lower()):
                return user
        return None

    # -- what a person sees --------------------------------------------
    def view(self, user: dict | None) -> View:
        key = ("*",) if not user or user.get("projects") == "*" else user["_slugs"]
        with self.lock:
            if key not in self._views:
                self._views[key] = View(self.edition if key == ("*",) else self._store.restrict(self.edition, key))
            return self._views[key]

    def progress(self, user: dict | None) -> ProgressFile:
        path = self.progress_path if not user else os.path.join(self.progress_dir, user["name"] + ".json")
        with self.lock:
            if path not in self._files:
                self._files[path] = ProgressFile(path)
            return self._files[path]

    def info(self, user: dict | None) -> dict:
        view = self.view(user)
        return {"edition": view.edition.get("edition", "public"), "cards": len(view.cards), "projects": len(view.projects),
                "user": user["name"] if user else "", "team": self.team, "assistant": self.client is not None}

    # -- the assistant --------------------------------------------------
    def allow_question(self, user: dict | None) -> int:
        """0 when this person may ask now, else the number of seconds to wait."""
        limit, window, now = self.settings["server"]["ask_per_10_minutes"], 600.0, time.monotonic()
        key = user["name"] if user else ""
        with self.lock:
            recent = [t for t in self._asked.get(key, []) if now - t < window]
            if len(recent) >= limit:
                self._asked[key] = recent
                return int(window - (now - recent[0])) + 1
            recent.append(now)
            self._asked[key] = recent
            return 0


OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Recurser", "version": "1",
             "description": "Flashcards about your own projects, each person's review history, and answers from the decks through the Anthropic SDK."},
    "paths": {
        "/healthz": {"get": {"summary": "Liveness. No sign-in.", "responses": {"200": {"description": "ok"}}}},
        "/api/health": {"get": {"summary": "Who is signed in and what the server offers.",
                                "responses": {"200": {"description": "edition, cards, projects, user, team, assistant"}}}},
        "/api/progress": {
            "get": {"summary": "This person's review history.", "responses": {"200": {"description": "the history object"}}},
            "put": {"summary": "Merge a history into this person's stored one; the merged history is returned.",
                    "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                    "responses": {"200": {"description": "the merged history"}}}},
        "/api/ask": {"post": {
            "summary": "Answer a question from the decks this person may see, with the passages the answer rests on.",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "required": ["question"],
                "properties": {"question": {"type": "string", "maxLength": 2000},
                               "slug": {"type": "string", "description": "limit the search to one project"}}}}}},
            "responses": {"200": {"description": "text, segments (text with the passages cited), sources (passages), model"},
                          "429": {"description": "too many questions: Retry-After says when"},
                          "503": {"description": "the assistant is not configured on this server"}}}},
        "/api/critique": {"post": {
            "summary": "Compare a typed answer with a card's reference.",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "required": ["id", "answer"],
                "properties": {"id": {"type": "string", "description": "card id, e.g. tide-loader:31"},
                               "answer": {"type": "string", "maxLength": 4000}}}}}},
            "responses": {"200": {"description": "text, refused, truncated, model"}}}},
        "/data/bundle.js": {"get": {"summary": "The decks this person may see, as the script the app loads.",
                                    "responses": {"200": {"description": "JavaScript"}}}},
    },
}


def docs_page() -> bytes:
    """A plain, self-contained description of the API (no script, no outside resource)."""
    import html

    rows = []
    for path, methods in OPENAPI["paths"].items():
        for method, op in methods.items():
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            rows.append(f"<h2><code>{method.upper()} {html.escape(path)}</code></h2><p>{html.escape(op.get('summary', ''))}</p>"
                        + (f"<pre>{html.escape(json.dumps(body, indent=1))}</pre>" if body else "")
                        + "<ul>" + "".join(f"<li><code>{code}</code> {html.escape(r.get('description', ''))}</li>" for code, r in op["responses"].items()) + "</ul>")
    page = ("<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Recurser API</title>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#121924}"
            "code,pre{font:13px ui-monospace,Menlo,Consolas,monospace}pre{background:#eef1f5;padding:.7rem;border-radius:6px;overflow:auto}"
            "h2{font-size:1rem;margin:1.6rem 0 .2rem}</style><h1>Recurser API</h1>"
            "<p>Sign in first with your personal link. The machine-readable description is at <a href='openapi.json'>openapi.json</a>.</p>"
            + "".join(rows) + "</html>")
    return page.encode("utf-8")


def make_handler(app_dir: str, site: Site):
    app_dir = os.path.realpath(app_dir)
    token = site.token

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

        def _json(self, status: int, data, extra: dict | None = None):
            self._send(status, json.dumps(data, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8", extra)

        def _cookie(self, value: str) -> dict:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
            return {"Set-Cookie": f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000{secure}"}

        def _cookie_value(self) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return value
            return ""

        def _who(self, query: dict) -> tuple[bool, dict | None, dict]:
            """(allowed, user, headers to add).  ``user`` is None when the server is not serving a team."""
            supplied = (query.get("token") or [""])[0]
            if site.team:
                if site.user_header:                       # an authenticating proxy in front names the person
                    user = site.by_name(self.headers.get(site.user_header, ""))
                    if user:
                        return True, user, {}
                user = site.by_token(supplied) if supplied else None
                if user:
                    return True, user, self._cookie(supplied)
                user = site.by_token(self._cookie_value()) if self._cookie_value() else None
                return (True, user, {}) if user else (False, None, {})
            if not token:
                return True, None, {}
            if supplied and hmac.compare_digest(supplied, token):
                return True, None, self._cookie(token)
            if self._cookie_value() and hmac.compare_digest(self._cookie_value(), token):
                return True, None, {}
            return False, None, {}

        def _foreign(self) -> bool:
            """A request another web page caused, or one that reached us under a foreign name (DNS rebinding)."""
            # Opening the page from a link elsewhere is fine; being embedded or fetched by another page is not.
            if (self.headers.get("Sec-Fetch-Site", "none").lower() in ("cross-site", "same-site")
                    and self.headers.get("Sec-Fetch-Mode", "").lower() != "navigate"):
                return True
            if token is None and not site.team:   # loopback mode: the page is only ever opened under a loopback name
                port = self.server.server_address[1]
                names = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]")} | ({"127.0.0.1", "localhost", "[::1]"} if port == 80 else set())
                if self.headers.get("Host", "") not in names:
                    return True
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc not in names:
                    return True
            return False

        def _body(self, limit: int):
            """The JSON object sent with the request, or None after an error answer was sent."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > limit:
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"the body must be 1 to {limit} bytes of JSON"})
                return None

            def refuse(name):
                raise ValueError(name)

            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=refuse)
            except (UnicodeDecodeError, ValueError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "the body is not valid JSON"})
                return None
            if not isinstance(data, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "the body must be a JSON object"})
                return None
            return data

        # -- routes ------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/healthz":                     # liveness and readiness probes: no sign-in, no data
                return self._send(200, b"ok\n", "text/plain; charset=utf-8")
            if self._foreign():
                return self._send(HTTPStatus.FORBIDDEN, b"This page only answers its own tab.", "text/plain; charset=utf-8")
            ok, user, extra = self._who(parse_qs(url.query))
            if not ok:
                hint = b"Open your personal link (it carries your token)." if site.team else b"Open the link printed by `recurser serve` (it carries the token)."
                return self._send(HTTPStatus.UNAUTHORIZED, hint, "text/plain; charset=utf-8")
            if url.path == "/api/health":
                return self._json(200, {"ok": True, **site.info(user)})
            if url.path == "/api/progress":
                return self._json(200, site.progress(user).read())
            if url.path == "/openapi.json":
                return self._json(200, OPENAPI)
            if url.path == "/docs":
                return self._send(200, docs_page(), "text/html; charset=utf-8")
            if url.path == "/data/bundle.js":              # built in memory for this person: nothing is written to disk
                return self._send(200, site.view(user).bundle_js, "application/javascript; charset=utf-8", extra)
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
            if self._foreign():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin write refused"})
            ok, user, _ = self._who(parse_qs(url.query))
            if not ok:
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "token required"})
            if url.path == "/api/progress":
                incoming = self._body(MAX_BODY)
                if incoming is None:
                    return None
                if not isinstance(incoming.get("cards", {}), dict):
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "expected a progress object with a 'cards' map"})
                return self._json(200, site.progress(user).merge_in(incoming))
            if self.command == "POST" and url.path in ("/api/ask", "/api/critique"):
                return self._assistant(url.path, user)
            return self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})

        do_POST = do_PUT

        def _assistant(self, path: str, user: dict | None):
            from . import assistant

            if site.client is None:
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "the assistant is not configured on this server"})
            data = self._body(64 * 1024)
            if data is None:
                return None
            view, llm = site.view(user), site.settings["llm"]
            if path == "/api/ask":
                question = data.get("question")
                if not isinstance(question, str) or not question.strip() or len(question) > 2000:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "'question' must be 1 to 2000 characters"})
                slug = data.get("slug")
                if slug is not None and slug not in view.projects:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "unknown project"})
            else:
                card, typed = view.cards.get(str(data.get("id"))), data.get("answer")
                if card is None:
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "unknown card"})
                if not isinstance(typed, str) or not typed.strip() or len(typed) > 4000:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "'answer' must be 1 to 4000 characters"})
            wait = site.allow_question(user)
            if wait:
                return self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many questions for now", "retry_after": wait}, {"Retry-After": str(wait)})
            if not site.slots.acquire(timeout=30):
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "the assistant is busy, try again in a moment"}, {"Retry-After": "10"})
            try:
                options = dict(client=site.client, model=llm["model"], max_tokens=llm["max_tokens"], fallbacks=llm["fallbacks"])
                if path == "/api/ask":
                    found = view.index.search(question, k=site.settings["retrieval"]["top_k"], slugs=[slug] if slug else None,
                                              per_project=site.settings["retrieval"]["per_project"])
                    result = assistant.ask(question.strip(), found, effort=llm["effort"], **options)
                    by_id = {p["id"]: p for p in found}
                    result["sources"] = [{k: by_id[pid].get(k) for k in ("id", "kind", "card_id", "slug", "code", "title", "path")}
                                         | {"text": "\n".join(by_id[pid]["blocks"])} for pid in result["sources"] if pid in by_id]
                    result["retrieved"] = len(found)
                else:
                    result = assistant.critique(card, view.projects.get(card["slug"], {}), typed, **options)
            except assistant.AssistantError as exc:
                extra = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
                return self._json(exc.status, {"error": str(exc)}, extra)
            except Exception as exc:      # an answer must end in an answer, never in a dropped connection
                return self._json(HTTPStatus.BAD_GATEWAY, {"error": f"the assistant failed ({type(exc).__name__})"})
            finally:
                site.slots.release()
            return self._json(200, result)

    return Handler


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def create_server(app_dir: str, *, host: str = "127.0.0.1", port: int = 8765, progress_path: str | None = None,
                  token: str | None = None, info: dict | None = None, site: Site | None = None,
                  edition: dict | None = None) -> tuple[ThreadingHTTPServer, str | None]:
    """Build the server (not yet serving).  Returns (server, token-or-None).

    Pass a ready ``site`` for a team; otherwise one is made from ``edition`` for a single person.
    """
    if site is None:
        if edition is None:
            raise ValueError("create_server needs an edition or a site")
        if not is_loopback(host) and not token:
            token = secrets.token_urlsafe(18)
        if is_loopback(host):
            token = token or None
        site = Site(edition, progress_path=progress_path, token=token)
    return ThreadingHTTPServer((host, port), make_handler(app_dir, site)), site.token
