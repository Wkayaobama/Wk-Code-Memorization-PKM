#!/usr/bin/env python3
"""Schema, grounding and safety validators for Recurser project maps and card files.

Stdlib only and import-free inside the package, so it runs both as a module and
as a plain script (including under ``python3 -I``)::

    python3 recurser/schema.py map   maps/tide-loader.json  --repo /path/to/checkout
    python3 recurser/schema.py cards cards/tide-loader.json --repo /path/to/checkout [--repo /alt/worktree]

Three kinds of checks:

* **shape**      required keys, types, lengths, enumerations, unique addresses;
* **grounding**  every cited ``path[:line]`` must exist in a checkout, and every
                 card's ``evidence_quote`` must appear verbatim in a cited file;
* **safety**     no credentials, tokens or personal e-mail addresses anywhere.

Errors make the file unusable (exit code 1). Warnings are advice.
"""
from __future__ import annotations

import json
import os
import re
import sys

# --------------------------------------------------------------------------
# Taxonomy — the address space.  A card's address is PP C I:
#   PP = project code (assigned once by the pipeline, never renumbered)
#   C  = category digit (below)       I = rank inside the category, 0 = most fundamental
# The category digits deliberately reuse the digit mnemonics of the owner's
# "Programming Millenium" matrix, so the same digit means the same kind of idea.
# --------------------------------------------------------------------------
CATEGORIES = {
    0: ("Purpose & fundamentals", "the origin: why it exists, for whom, the core idea"),
    1: ("Data model & contracts", "a single node: entities, schemas, config keys, file formats"),
    2: ("Flow & algorithms", "split in two: pipeline order, core logic, transformations"),
    3: ("Functions & APIs", "input -> transform -> output: key functions, endpoints, CLI"),
    4: ("Components & modules", "four pillars: the building blocks and their responsibilities"),
    5: ("Runtime & orchestration", "five philosophers: async, concurrency, scheduling, jobs, agents"),
    6: ("Architecture & decisions", "six sides of the hexagon: the big design and the WHY"),
    7: ("Storage & persistence", "seven normal forms: databases, warehouses, files, state"),
    8: ("Infra & integrations", "port 80: cloud, deployment, CI, external APIs, auth"),
    9: ("Quality, ops & directions", "five nines: tests, failure modes, runbooks, next steps"),
}

CARD_TYPES = {
    "recall": "direct question -> short fact",
    "locate": "which function / file / command does X",
    "output": "predict the output (or the intended error) of a call or snippet",
    "why": "design rationale: why X rather than Y",
    "trace": "ordered chain: recite the steps in order",
    "cloze": "fill the blank in a signature, command, config or statement",
    "compare": "discriminate two confusable things by their one difference",
    "scenario": "apply it: something breaks or must change, what do you do",
}

LEVELS = {
    1: "on the go: one-line recall",
    2: "understanding: explain a mechanism",
    3: "application: trace or use it",
    4: "analysis: trade-offs, what-if, new directions",
}

CARD_FIELDS = {
    "category", "rank", "type", "level", "concept", "question", "answer", "explanation", "anchor", "example",
    "confusables", "cloze", "code_snippet", "expected_output", "steps", "source", "evidence_quote", "tags",
    "source_refs",  # added at ingest: the branch / commit that holds each cited path
    "see_also",     # optional cross-links: up to four card ids ("<slug>:<category><rank>"), usually in OTHER projects
}
SEE_ALSO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*:\d\d$")
SEE_ALSO_MAX = 4
GROUNDING_TAGS = {"grounded:test", "grounded:fixture", "grounded:doc", "grounded:logic"}
YES_NO_OPENERS = ("does ", "do ", "is ", "are ", "can ", "should ", "will ", "has ", "have ", "did ")

MAP_STATUS = {"active", "paused", "archived", "scaffold", "docs-only", "empty"}
CONFIDENCE = {"high", "medium", "low"}

# --------------------------------------------------------------------------
# Safety scanning
# --------------------------------------------------------------------------
SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")),
    ("Anthropic key", re.compile(r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("OpenAI-style key", re.compile(r"(?<![A-Za-z0-9])sk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}|[A-Za-z0-9]{24,})")),
    ("Stripe key", re.compile(r"(?<![A-Za-z0-9])[sr]k_live_[A-Za-z0-9]{16,}")),
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}")),
    ("GitHub fine-grained token", re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{30,}")),
    ("Slack token", re.compile(r"(?<![A-Za-z0-9])(?:xox[abeoprs]|xapp)-[A-Za-z0-9\-]{10,}")),
    ("Slack webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]{20,}")),
    ("Google API key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{35}")),
    ("Google OAuth secret", re.compile(r"(?<![A-Za-z0-9])(?:GOCSPX-[A-Za-z0-9_\-]{20,}|ya29\.[A-Za-z0-9_\-]{20,})")),
    ("HubSpot private app token", re.compile(r"(?<![A-Za-z0-9])pat-[a-z]{2}\d-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("JWT", re.compile(r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+(?=[A-Za-z0-9._~+/\-]*\d)(?=[A-Za-z0-9._~+/\-]*[A-Za-z])[A-Za-z0-9._~+/\-]{24,}=*")),
]

# name [:=] value, where the name CONTAINS a credential word (DB_PASSWORD, hubspotApiKey, "client_secret": ...)
_ASSIGNED = re.compile(
    r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|api[_\-]?key|access[_\-]?key|private[_\-]?key|token|credential)"
    r"[A-Za-z0-9_\-]*[\"']?\s*(?::|=|=>)\s*[\"']?(?P<value>[^\s\"',;)}\]]{8,})"
)
# scheme://user:password@host
_URL_PASSWORD = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:(?P<value>[^@\s/]{3,})@[A-Za-z0-9.\-]+")
_PLACEHOLDER = re.compile(
    r"(?i)(?:^\$|\$\{|\$\(|^<|>$|^\{|\}$|^%|%$|\*{3}|\.{3}|x{4,}|^(?:none|null|true|false|undefined|string|str|bytes|secret|password|token|changeme|redacted)$"
    r"|your|example|placeholder|dummy|fake|sample|redacted|environ|getenv|process\.env|settings\.|config\.|self\.|args\.|os\.|secrets?\.|vault|keyring|\(\)?$)"
)


def _looks_secret(value: str) -> bool:
    """A literal that could be a real credential: long enough, mixed, and not a placeholder or a reference."""
    value = value.strip("\"'`")
    if len(value) < 8 or "(" in value or _PLACEHOLDER.search(value):      # a call such as Text.From(...) is code, not a literal
        return False
    digits = len(re.findall(r"\d", value))
    if digits < 4 and (re.fullmatch(r"[A-Z][A-Z0-9_]*", value) or re.fullmatch(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*", value)):
        return False                      # another variable, an attribute path, a plain word
    classes = sum(bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    return (len(value) >= 12 and classes >= 2 and bool(re.search(r"\d", value))) or (len(value) >= 20 and classes >= 3)


EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
EMAIL_ALLOW = re.compile(
    r"(?i)(@example\.(?:com|org|net)$|@(?:[a-z0-9\-]+\.)*(?:test|invalid|localhost|local)$|@users\.noreply\.github\.com$|^noreply@|^git@|"
    r"\.iam\.gserviceaccount\.com$)"
)


def scan_text(text: str) -> list[str]:
    """Return a list of safety findings for one string."""
    out = []
    for label, pat in SECRET_PATTERNS:
        if pat.search(text):
            out.append(f"possible secret ({label})")
    if not out:
        if any(_looks_secret(m.group("value")) for m in _ASSIGNED.finditer(text)):
            out.append("possible secret (hard-coded credential)")
        elif any(_looks_secret(m.group("value")) or len(m.group("value")) >= 6 and not _PLACEHOLDER.search(m.group("value"))
                 and m.group("value").lower() not in ("password", "passwd", "pass", "secret", "pwd")
                 for m in _URL_PASSWORD.finditer(text)):
            out.append("possible secret (password inside a URL)")
    for m in EMAIL_RE.finditer(text):
        addr = m.group(0)
        if not EMAIL_ALLOW.search(addr):
            out.append(f"e-mail address present ({addr[:3]}***@{addr.split('@')[1]})")
    return out


def iter_strings(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from iter_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from iter_strings(v, f"{path}[{i}]")


# --------------------------------------------------------------------------
# Grounding helpers
# --------------------------------------------------------------------------
SOURCE_RE = re.compile(r"^(?P<path>[^:]+?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?$")
_WS = re.compile(r"\s+")
_file_cache: dict[str, str | None] = {}


def _read(path: str) -> str | None:
    if path not in _file_cache:
        try:
            if os.path.getsize(path) > 4_000_000:
                _file_cache[path] = None
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    _file_cache[path] = fh.read()
        except OSError:
            _file_cache[path] = None
    return _file_cache[path]


def split_source(ref: str):
    """'a/b.py:10-20' -> ('a/b.py', 10, 20).  Returns None when malformed."""
    m = SOURCE_RE.match(ref.strip())
    if not m:
        return None
    a = int(m.group("a")) if m.group("a") else None
    b = int(m.group("b")) if m.group("b") else a
    if a is not None and b is not None and b < a:
        return None
    rel = m.group("path").strip()
    while rel.startswith("./"):  # drop a leading "./" only — dot-directories such as .claude/ or .github/ are real paths
        rel = rel[2:]
    return rel, a, b


def resolve_source(ref: str, repos: list[str]):
    """Find the checkout that contains the cited path. -> (abs_path, repo_dir) or (None, reason)."""
    parts = split_source(ref)
    if not parts:
        return None, f"malformed source reference {ref!r} (want path or path:line or path:start-end)"
    rel, a, b = parts
    if rel.startswith("/") or ".." in rel.split("/"):
        return None, f"source {ref!r} must be a path relative to the repository root"
    past_end = None
    for repo in repos:
        full = os.path.join(repo, rel)
        if not os.path.exists(full):
            continue
        root = os.path.realpath(repo)
        if os.path.commonpath([root, os.path.realpath(full)]) != root:
            return None, f"source {ref!r} leaves the checkout through a link"
        if a is not None and os.path.isfile(full):
            text = _read(full)
            if text is not None:
                n = len(text.splitlines())
                if a < 1 or (b or a) > n:
                    past_end = f"source {ref!r}: line {b or a} is past the end of the file ({n} lines)"
                    continue          # another line of work may hold a longer version of the same path
        return full, repo
    return None, past_end or f"source {ref!r}: path not found in the checkout(s)"


def resolve_all(ref: str, repos: list[str]) -> list[str]:
    """Every checkout's copy of a cited path (a path shared by two lines of work exists in both)."""
    out = []
    for repo in repos:
        full, _ = resolve_source(ref, [repo])
        if full:
            out.append(full)
    return out


def quote_found(quote: str, files: list[str]) -> bool:
    needle = _WS.sub(" ", quote).strip()
    if not needle:
        return False
    for f in files:
        if os.path.isdir(f):
            continue
        text = _read(f)
        if text and needle in _WS.sub(" ", text):
            return True
    return False


# --------------------------------------------------------------------------
# Generic shape checking
# --------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    @property
    def ok(self):
        return not self.errors


def _str(rep, obj, key, where, *, required=True, max_len=None, min_len=1):
    v = obj.get(key)
    if v is None or v == "":
        if required:
            rep.err(f"{where}: missing '{key}'")
        return ""
    if not isinstance(v, str):
        rep.err(f"{where}: '{key}' must be a string")
        return ""
    if len(v.strip()) < min_len:
        rep.err(f"{where}: '{key}' is too short")
    if max_len and len(v) > max_len:
        rep.err(f"{where}: '{key}' is {len(v)} chars, limit {max_len}")
    return v


def _list(rep, obj, key, where, *, required=True, min_items=0, max_items=None):
    v = obj.get(key)
    if v is None:
        if required:
            rep.err(f"{where}: missing '{key}' (use [] when there is nothing)")
        return []
    if not isinstance(v, list):
        rep.err(f"{where}: '{key}' must be a list")
        return []
    if len(v) < min_items:
        rep.err(f"{where}: '{key}' needs at least {min_items} item(s), has {len(v)}")
    if max_items is not None and len(v) > max_items:
        rep.err(f"{where}: '{key}' has {len(v)} items, limit {max_items}")
    return v


def _safety(rep, obj, label):
    for path, text in iter_strings(obj):
        for finding in scan_text(text):
            rep.err(f"{label} {path}: {finding} — remove it (never copy credentials or personal data)")


# --------------------------------------------------------------------------
# Project map
# --------------------------------------------------------------------------
def validate_map(data: dict, repos: list[str] | None = None) -> Report:
    rep = Report()
    repos = repos or []
    if not isinstance(data, dict):
        rep.err("top level must be a JSON object")
        return rep
    w = "map"
    _str(rep, data, "slug", w, max_len=80)
    _str(rep, data, "repo", w, max_len=120)
    _str(rep, data, "title", w, max_len=80)
    _str(rep, data, "one_liner", w, max_len=200, min_len=15)
    _str(rep, data, "purpose", w, max_len=1500, min_len=40)
    _str(rep, data, "domain", w, max_len=80)
    if data.get("status") not in MAP_STATUS:
        rep.err(f"map: 'status' must be one of {sorted(MAP_STATUS)}")
    if data.get("confidence") not in CONFIDENCE:
        rep.err(f"map: 'confidence' must be one of {sorted(CONFIDENCE)}")
    _str(rep, data, "coverage_notes", w, max_len=1200)

    for s in _list(rep, data, "stack", w, min_items=1, max_items=30):
        if not isinstance(s, str) or len(s) > 80:
            rep.err("map: every 'stack' entry must be a short string")
    for s in _list(rep, data, "aliases", w, max_items=12):
        if not isinstance(s, str) or len(s) > 60:
            rep.err("map: every 'aliases' entry must be a short string")
    for s in _list(rep, data, "data_flow", w, max_items=20):
        if not isinstance(s, str) or len(s) > 400:
            rep.err("map: every 'data_flow' step must be a string of at most 400 chars")
    for s in _list(rep, data, "directions", w, max_items=12):
        if not isinstance(s, str) or len(s) > 400:
            rep.err("map: every 'directions' entry must be a string of at most 400 chars")

    def records(key, spec, *, min_items=0, max_items=40, path_keys=()):
        items = _list(rep, data, key, w, min_items=min_items, max_items=max_items)
        for i, it in enumerate(items):
            where = f"map.{key}[{i}]"
            if not isinstance(it, dict):
                rep.err(f"{where}: must be an object")
                continue
            for k, (required, limit) in spec.items():
                _str(rep, it, k, where, required=required, max_len=limit)
            for pk in path_keys:
                ref = it.get(pk)
                if ref and repos:
                    full, why = resolve_source(ref, repos)
                    if not full:
                        rep.err(f"{where}.{pk}: {why}")
            extra = it.get("talks_to")
            if extra is not None and not (isinstance(extra, list) and all(isinstance(x, str) for x in extra)):
                rep.err(f"{where}: 'talks_to' must be a list of strings")

    records("entry_points", {"command": (True, 300), "what": (True, 300), "path": (False, 200)}, path_keys=("path",))
    records(
        "architecture",
        {"component": (True, 80), "path": (True, 200), "responsibility": (True, 400)},
        min_items=1,
        path_keys=("path",),
    )
    records(
        "key_functions",
        {
            "name": (True, 120),
            "path": (True, 200),
            "signature": (False, 400),
            "does": (True, 500),
            "input_example": (False, 400),
            "expected_output": (False, 500),
        },
        path_keys=("path",),
    )
    records("infra", {"topic": (True, 100), "detail": (True, 600), "path": (False, 200)}, path_keys=("path",))
    records(
        "decisions",
        {"decision": (True, 300), "why": (True, 600), "alternative_rejected": (False, 300), "evidence": (False, 200)},
        path_keys=("evidence",),
    )
    records(
        "gotchas",
        {"issue": (True, 300), "consequence": (False, 400), "fix_or_guard": (False, 400), "path": (False, 200)},
        path_keys=("path",),
    )
    records("glossary", {"term": (True, 80), "meaning": (True, 400)}, max_items=40)
    records("relations", {"project": (True, 80), "relation": (True, 400)}, max_items=20)

    status = data.get("status")
    if status not in ("empty", "docs-only", "scaffold"):
        if len(data.get("key_functions") or []) < 3:
            rep.warn("map: fewer than 3 key_functions for a code project — is the map complete?")
        if len(data.get("data_flow") or []) < 3:
            rep.warn("map: data_flow has fewer than 3 steps")
        if not data.get("decisions"):
            rep.warn("map: no design decisions recorded (the WHY is what gets forgotten first)")
    _safety(rep, data, "map")
    return rep


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------
CLOZE_RE = re.compile(r"\{\{c\d+::[^{}]+?\}\}")


def card_key(card: dict) -> str:
    return f"{card.get('category')}{card.get('rank')}"


def validate_cards(data: dict, repos: list[str] | None = None, *, min_cards=6, max_cards=60) -> Report:
    rep = Report()
    repos = repos or []
    if not isinstance(data, dict):
        rep.err("top level must be a JSON object with 'slug' and 'cards'")
        return rep
    _str(rep, data, "slug", "cards file", max_len=80)
    cards = _list(rep, data, "cards", "cards file", min_items=min_cards, max_items=max_cards)
    retired = data.get("retired", [])
    if not (isinstance(retired, list) and all(isinstance(x, str) and re.match(r"^\d\d$", x) for x in retired)):
        rep.err("cards file: 'retired' must be a list of two-digit address keys such as \"31\"")
        retired = []
    seen: dict[str, int] = {}
    questions: dict[str, int] = {}
    concepts: dict[str, int] = {}
    for i, c in enumerate(cards):
        where = f"card[{i}]"
        if not isinstance(c, dict):
            rep.err(f"{where}: must be an object")
            continue
        cat, rank = c.get("category"), c.get("rank")
        if not (isinstance(cat, int) and not isinstance(cat, bool) and 0 <= cat <= 9):
            rep.err(f"{where}: 'category' must be an integer 0-9")
        if not (isinstance(rank, int) and not isinstance(rank, bool) and 0 <= rank <= 9):
            rep.err(f"{where}: 'rank' must be an integer 0-9 (0 = most fundamental in its category)")
        key = card_key(c)
        where = f"card[{i}] (key {key})"
        if key in seen:
            rep.err(f"{where}: address already used by card[{seen[key]}] — each (category, rank) is one slot")
        seen[key] = i
        if key in retired:
            rep.err(f"{where}: address {key} was retired — a retired address is never reused, take the next free rank")
        unknown = sorted(set(c) - CARD_FIELDS)
        if unknown:
            rep.warn(f"{where}: fields outside the contract are dropped at build time: {', '.join(unknown)}")
        ctype = c.get("type")
        if ctype not in CARD_TYPES:
            rep.err(f"{where}: 'type' must be one of {sorted(CARD_TYPES)}")
        level = c.get("level")
        if level not in LEVELS:
            rep.err(f"{where}: 'level' must be 1, 2, 3 or 4")

        _str(rep, c, "concept", where, max_len=80, min_len=3)
        q = _str(rep, c, "question", where, max_len=420, min_len=12)
        a = _str(rep, c, "answer", where, max_len=700, min_len=1)
        _str(rep, c, "explanation", where, required=False, max_len=900)
        _str(rep, c, "anchor", where, required=False, max_len=320)
        _str(rep, c, "example", where, required=False, max_len=900)
        _str(rep, c, "confusables", where, required=False, max_len=420)
        links = c.get("see_also")
        if links is not None:
            if not (isinstance(links, list) and all(isinstance(x, str) and SEE_ALSO_RE.match(x) for x in links)):
                rep.err(f"{where}: 'see_also' must be a list of card ids such as \"tide-loader:31\"")
            elif len(links) > SEE_ALSO_MAX or len(set(links)) != len(links):
                rep.err(f"{where}: 'see_also' holds at most {SEE_ALSO_MAX} distinct card ids")
        if q:
            norm = _WS.sub(" ", q.lower()).strip()
            if norm in questions:
                rep.err(f"{where}: duplicate question (same as card[{questions[norm]}])")
            questions[norm] = i
        if level == 1 and a and len(a) > 260:
            rep.warn(f"{where}: level-1 answer is {len(a)} chars — on-the-go cards should answer in one line")
        if q and q.lower().startswith(YES_NO_OPENERS):
            rep.warn(f"{where}: the question opens like a yes/no question — ask which, what, why, how many")
        if q and "according to" in q.lower():
            rep.warn(f"{where}: 'according to ...' asks about a document, not about the project")
        name = _WS.sub(" ", str(c.get("concept", "")).lower()).strip()
        if name:
            if name in concepts:
                rep.warn(f"{where}: concept repeats card[{concepts[name]}] — one concept, one home")
            concepts[name] = i

        if ctype == "output":
            _str(rep, c, "code_snippet", where, max_len=900, min_len=5)
            _str(rep, c, "expected_output", where, max_len=600)
            grounded = GROUNDING_TAGS & set(c.get("tags") or []) if isinstance(c.get("tags"), list) else set()
            if len(grounded) != 1:
                rep.warn(f"{where}: an output card should carry exactly one of {sorted(GROUNDING_TAGS)}")
        if ctype == "cloze":
            cl = _str(rep, c, "cloze", where, max_len=600, min_len=10)
            if cl and not CLOZE_RE.search(cl):
                rep.err(f"{where}: 'cloze' needs at least one {{{{c1::hidden text}}}} deletion")
            elif cl and ("{{" in CLOZE_RE.sub("", cl) or "}}" in CLOZE_RE.sub("", cl)):
                rep.warn(f"{where}: braces outside the deletions (Jinja, f-strings) will confuse cloze renderers")
        if ctype == "trace":
            steps = _list(rep, c, "steps", where, min_items=3, max_items=12)
            if len(steps) > 9:
                rep.warn(f"{where}: {len(steps)} steps — a chain longer than 9 is better split into two traces")
            for s in steps:
                if not isinstance(s, str) or not s.strip() or len(s) > 260:
                    rep.err(f"{where}: every step must be a non-empty string of at most 260 chars")
        if ctype == "compare" and not c.get("confusables"):
            rep.err(f"{where}: a 'compare' card must fill 'confusables' (the look-alike and the one difference)")
        if ctype == "why" and not c.get("explanation"):
            rep.warn(f"{where}: a 'why' card usually deserves an 'explanation'")

        tags = c.get("tags", [])
        if not (isinstance(tags, list) and all(isinstance(t, str) and len(t) <= 40 for t in tags)):
            rep.err(f"{where}: 'tags' must be a list of short strings")

        sources = _list(rep, c, "source", where, min_items=1, max_items=6)
        resolved = []
        for ref in sources:
            if not isinstance(ref, str):
                rep.err(f"{where}: every source must be a string 'path' or 'path:line'")
                continue
            if repos:
                full, why = resolve_source(ref, repos)
                if not full:
                    rep.err(f"{where}: {why}")
                else:
                    resolved.extend(resolve_all(ref, repos))   # a shared path may differ between lines of work
            elif not split_source(ref):
                rep.err(f"{where}: malformed source reference {ref!r}")
        quote = _str(rep, c, "evidence_quote", where, max_len=260, min_len=12)
        if quote and repos and resolved and not quote_found(quote, resolved):
            rep.err(
                f"{where}: evidence_quote was not found verbatim in the cited source file(s) — "
                "copy an exact line or phrase from the file that supports the answer"
            )

    _safety(rep, data, "cards")

    # Coverage advice — a deck should climb the difficulty ladder and vary its question forms.
    valid = [c for c in cards if isinstance(c, dict)]
    if valid:
        cats = {c.get("category") for c in valid}
        types = {c.get("type") for c in valid}
        levels = [c.get("level") for c in valid]
        if "00" not in seen:
            rep.warn("no card at address 00 — the deck should open with 'what is this project and why does it exist'")
        if len(cats) < 4:
            rep.warn(f"only {len(cats)} categories used — spread the deck over the taxonomy")
        if len(types) < 3:
            rep.warn(f"only {len(types)} question forms used — vary recall / locate / output / why / trace / cloze / compare / scenario")
        n = len(valid)
        if levels.count(1) < 0.25 * n:
            rep.warn("fewer than 25% level-1 cards — the owner revises on the go and needs quick ones")
        if levels.count(3) + levels.count(4) < 0.15 * n:
            rep.warn("fewer than 15% level-3/4 cards — add some trace / scenario / why depth")
    return rep


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("map", "cards"):
        print(__doc__)
        return 2
    kind, path = argv[1], argv[2]
    repos, i = [], 3
    while i < len(argv):
        if argv[i] == "--repo" and i + 1 < len(argv):
            repos.append(argv[i + 1])
            i += 2
        else:
            print(f"unknown argument {argv[i]!r}")
            return 2
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INVALID  {path}: cannot read JSON — {exc}")
        return 1
    rep = validate_map(data, repos) if kind == "map" else validate_cards(data, repos)
    for msg in rep.errors:
        print("ERROR  ", msg)
    for msg in rep.warnings:
        print("WARNING", msg)
    count = len(data.get("cards", [])) if kind == "cards" and isinstance(data, dict) else ""
    verdict = "OK" if rep.ok else "INVALID"
    print(f"{verdict}  {kind} {path}  errors={len(rep.errors)} warnings={len(rep.warnings)}" + (f" cards={count}" if count != "" else ""))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
