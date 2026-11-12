"""Fuzzy project matching — stdlib only.

The owner rarely says a repository's exact name: "headless sonar" means
``Bhead-Sonar``, "etl big q" means ``KP-ETL-BQ``.  This matcher scores a spoken
nickname against every field a project carries (name, aliases, title, one-liner,
keywords) and explains why a candidate matched.

Signals, per query token:

* exact token                         1.00
* prefix either way (>= 3 chars)      0.85
* typo-tolerant ratio >= 0.75         ratio * 0.9
* shared substring (>= 4 chars)       0.60
* acronym: initials of consecutive tokens on one side equal a token on the
  other side ("big q" <-> "bq")       0.90

The field score blends token coverage (75%) with a character-trigram Dice
coefficient over the squashed strings (25%), then is weighted by field.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

FIELD_WEIGHTS = {
    "name": 1.0,
    "alias": 1.0,
    "title": 0.9,
    "keyword": 0.75,
    "one_liner": 0.6,
}

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOP = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "my", "project", "repo", "thing", "app"}


def tokens(text: str, *, keep_stop: bool = False) -> list[str]:
    """'KP-ETL-BQ' -> ['kp','etl','bq'];  'HarborBQ' -> ['harbor','bq']."""
    out: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", text or ""):
        if not chunk:
            continue
        parts = _CAMEL.findall(chunk) or [chunk]
        out.extend(p.lower() for p in parts)
    if not keep_stop:
        kept = [t for t in out if t not in _STOP]
        out = kept or out
    return out


def squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i : i + 3] for i in range(len(s) - 2)}


def trigram_dice(a: str, b: str) -> float:
    a, b = squash(a), squash(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = _trigrams(a), _trigrams(b)
    return 2 * len(ta & tb) / (len(ta) + len(tb))


def _common_substring(a: str, b: str) -> int:
    m = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return m.size


def token_similarity(q: str, c: str) -> float:
    if q == c:
        return 1.0
    if min(len(q), len(c)) >= 3 and (c.startswith(q) or q.startswith(c)):
        return 0.85
    ratio = SequenceMatcher(None, q, c, autojunk=False).ratio()
    if ratio >= 0.75:
        return ratio * 0.9
    if _common_substring(q, c) >= 4:
        return 0.6
    return 0.0


def _acronym_hits(q_tokens: list[str], c_tokens: list[str]) -> dict[int, float]:
    """Indices of query tokens explained by an acronym relation with the candidate."""
    hits: dict[int, float] = {}
    c_set = set(c_tokens)
    # initials of consecutive QUERY tokens equal one candidate token: "big q" -> "bq"
    for n in (4, 3, 2):
        for i in range(len(q_tokens) - n + 1):
            initials = "".join(t[0] for t in q_tokens[i : i + n])
            if initials in c_set:
                for j in range(i, i + n):
                    hits[j] = max(hits.get(j, 0.0), 0.9)
    # initials of consecutive CANDIDATE tokens equal one query token: "bq" -> "big query"
    for n in (4, 3, 2):
        for i in range(len(c_tokens) - n + 1):
            initials = "".join(t[0] for t in c_tokens[i : i + n])
            for j, qt in enumerate(q_tokens):
                if qt == initials:
                    hits[j] = max(hits.get(j, 0.0), 0.9)
    return hits


def field_score(query: str, text: str) -> float:
    """0..1 similarity between a spoken query and one field of a candidate."""
    q_tokens, c_tokens = tokens(query), tokens(text)
    if not q_tokens or not c_tokens:
        return 0.0
    if squash(query) == squash(text):
        return 1.0
    acr = _acronym_hits(q_tokens, c_tokens)
    per_token = []
    for i, qt in enumerate(q_tokens):
        best = max((token_similarity(qt, ct) for ct in c_tokens), default=0.0)
        per_token.append(max(best, acr.get(i, 0.0)))
    coverage = sum(per_token) / len(per_token)
    return round(0.75 * coverage + 0.25 * trigram_dice(query, text), 4)


def candidate_fields(item: dict) -> list[tuple[str, str]]:
    """(field kind, text) pairs for one project / repository record."""
    fields: list[tuple[str, str]] = []
    for key in ("name", "repo", "slug"):
        if item.get(key):
            fields.append(("name", str(item[key]).split("/")[-1]))
    for alias in item.get("aliases") or []:
        fields.append(("alias", alias))
    if item.get("title"):
        fields.append(("title", item["title"]))
    for kw in item.get("keywords") or []:
        fields.append(("keyword", kw))
    if item.get("one_liner"):
        fields.append(("one_liner", item["one_liner"]))
    return fields


def score_item(query: str, item: dict) -> tuple[float, str]:
    """Best weighted field score and a short explanation."""
    best, why, strong = 0.0, "", 0
    for kind, text in candidate_fields(item):
        raw = field_score(query, text)
        if kind == "one_liner":
            # A sentence is long: reward coverage of the query, not overall similarity.
            q_tokens, c_tokens = tokens(query), tokens(text)
            if q_tokens and c_tokens:
                raw = sum(max(token_similarity(q, c) for c in c_tokens) for q in q_tokens) / len(q_tokens)
        s = raw * FIELD_WEIGHTS[kind]
        if s >= 0.55:
            strong += 1
        if s > best:
            best, why = s, f"{kind}: {text}"
    if strong > 1:
        best = min(1.0, best + 0.03 * (strong - 1))
    return round(best, 4), why


def rank(query: str, items: list[dict], *, limit: int = 5, threshold: float = 0.35) -> list[dict]:
    """Rank records for a query. Each result: {'item', 'score', 'why'}."""
    scored = []
    for item in items:
        s, why = score_item(query, item)
        if s >= threshold:
            scored.append({"item": item, "score": s, "why": why})
    scored.sort(key=lambda r: (-r["score"], str(r["item"].get("name") or r["item"].get("slug") or "")))
    return scored[:limit]


def best(query: str, items: list[dict], *, threshold: float = 0.45):
    hits = rank(query, items, limit=1, threshold=threshold)
    return hits[0] if hits else None
