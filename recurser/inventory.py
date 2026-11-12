"""Project inventory and last-accessed analysis.

"Last accessed" is the most recent push to ANY branch of a repository — the
closest observable trace of the owner's hands on the project.  From it we
derive a staleness tier and a forgetting estimate that drives what gets
rehearsed first.

The forgetting estimate is a planning heuristic, not a measurement::

    retention(t) = 0.5 ** (days_since_touch / half_life)
    half_life    = BASE_HALF_LIFE_DAYS * (1 + MASTERY_GAIN * mastery)

``mastery`` (0..1) grows with a card's review interval; an unrehearsed
project halves every three weeks, a fully rehearsed one every ~30 weeks.
This module scores a repository from its last push alone (nothing rehearsed
yet).  The web app applies the same formula card by card: an unseen card
fades from the project's last push, a reviewed card from its last review, and
a project's figure is the mean over its cards.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

BASE_HALF_LIFE_DAYS = 21.0
MASTERY_GAIN = 9.0

TIERS = [  # (max days since last push, key, label)
    (14, "hot", "in your hands"),
    (45, "warm", "still fresh"),
    (120, "cooling", "fading"),
    (365, "cold", "mostly gone"),
    (10**9, "frozen", "archaeology"),
]


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (``Z`` suffix accepted) into an aware UTC datetime."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def days_between(pushed_at: str, today: date) -> int:
    return max(0, (today - parse_ts(pushed_at).date()).days)


def tier_for(days: int) -> tuple[str, str]:
    for limit, key, label in TIERS:
        if days <= limit:
            return key, label
    return TIERS[-1][1], TIERS[-1][2]


def half_life(mastery: float = 0.0) -> float:
    mastery = min(1.0, max(0.0, mastery))
    return BASE_HALF_LIFE_DAYS * (1 + MASTERY_GAIN * mastery)


def retention(days_since_touch: float, mastery: float = 0.0) -> float:
    """Estimated share of project knowledge still retrievable (0..1)."""
    return round(0.5 ** (max(0.0, days_since_touch) / half_life(mastery)), 4)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def is_public(visibility) -> bool:
    """Fail closed: a repository is public only when it says exactly that.  ``PRIVATE``, ``internal``,
    a missing value, a boolean or a typo all count as private."""
    return isinstance(visibility, str) and visibility.strip().lower() == "public"


def normal_visibility(rec: dict) -> str:
    """'public' or 'private' for one record, from ``visibility`` or the ``private`` / ``isPrivate`` booleans."""
    visibility = rec.get("visibility")
    if visibility is None and ("private" in rec or "isPrivate" in rec):
        visibility = "private" if rec.get("private", rec.get("isPrivate")) else "public"
    return "public" if is_public(visibility) else "private"


def _norm(rec: dict) -> dict:
    """Normalise one repository record from any supported source."""
    name = rec.get("name") or (rec.get("full_name") or rec.get("nameWithOwner") or "").split("/")[-1]
    visibility = normal_visibility(rec)
    pushed = rec.get("pushed_at") or rec.get("pushedAt") or rec.get("updated_at") or ""
    out = {
        "name": name,
        "pushed_at": pushed,
        "visibility": visibility,
        "fork": bool(rec.get("fork", rec.get("isFork", False))),
    }
    for src, dst in (("description", "description"), ("language", "language"), ("html_url", "url"), ("url", "url"),
                     ("default_branch", "default_branch"), ("archived", "archived")):
        if rec.get(src) not in (None, "") and dst not in out:
            out[dst] = rec[src]
    return out


def load_snapshot(path: str) -> list[dict]:
    """Read an inventory snapshot: ``.tsv`` (name, pushed_at, visibility, fork) or ``.json``."""
    if path.endswith(".tsv"):
        repos = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh, delimiter="\t"):
                if not row or row[0].startswith("#"):
                    continue
                if len(row) < 3:
                    raise ValueError(f"{path}: a snapshot row needs name, pushed_at and visibility ({row!r})")
                repos.append({"name": row[0].strip(), "pushed_at": row[1].strip(), "visibility": "public" if is_public(row[2]) else "private",
                              "fork": len(row) > 3 and row[3].strip().lower() in ("1", "true", "yes")})
        return repos
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("repos") or data.get("repositories") or []
    return [_norm(r) for r in data if isinstance(r, dict)]


def analyze(repos: list[dict], today: date | None = None) -> list[dict]:
    """Add days_since / tier / unrehearsed retention to every repository, newest first."""
    today = today or datetime.now(timezone.utc).date()
    out = []
    for r in repos:
        if not r.get("pushed_at"):
            continue
        days = days_between(r["pushed_at"], today)
        key, label = tier_for(days)
        out.append({**r, "days_since": days, "tier": key, "tier_label": label, "retention": retention(days)})
    out.sort(key=lambda r: (r["days_since"], r["name"].lower()))
    return out


def select_recent(repos: list[dict], *, days: int = 180, include_forks: bool = False) -> list[dict]:
    """Repositories touched inside the window — the rehearsal candidates."""
    return [r for r in repos if r["days_since"] <= days and (include_forks or not r["fork"])]


# --------------------------------------------------------------------------
# Refreshing from GitHub (optional; the snapshot is always enough to run)
# --------------------------------------------------------------------------
def _gh_json(args: list[str]):
    proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh api failed")
    text = proc.stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # --paginate concatenates JSON arrays back to back
        out, dec, i = [], json.JSONDecoder(), 0
        while i < len(text):
            while i < len(text) and text[i].isspace():
                i += 1
            if i >= len(text):
                break
            obj, i = dec.raw_decode(text, i)
            out.extend(obj if isinstance(obj, list) else [obj])
        return out


def _http_json(url: str, token: str | None):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "recurser"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https host
        return json.load(resp)


def fetch_github(owner: str, *, token: str | None = None, max_pages: int = 10) -> list[dict]:
    """Fetch the owner's repositories with their last-push time.

    Tries the ``gh`` CLI first (uses the caller's own login, sees private
    repositories), then the REST API with ``GITHUB_TOKEN`` / ``GH_TOKEN``, then the
    anonymous REST API (public repositories only).  Raises ``RuntimeError`` when
    every route fails, so callers can keep the snapshot they already have.
    """
    errors = []
    if shutil.which("gh"):
        for path in ("user/repos?per_page=100&affiliation=owner&sort=pushed", f"users/{owner}/repos?per_page=100&sort=pushed"):
            try:
                data = _gh_json(["--paginate", path])
                if isinstance(data, list) and data:
                    return [_norm(r) for r in data if isinstance(r, dict)]
            except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
                errors.append(f"gh {path.split('?')[0]}: {str(exc)[:120]}")
    token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    base = "https://api.github.com/user/repos?affiliation=owner" if token else f"https://api.github.com/users/{owner}/repos?type=owner"
    try:
        repos = []
        for page in range(1, max_pages + 1):
            chunk = _http_json(f"{base}&per_page=100&sort=pushed&page={page}", token)
            if not isinstance(chunk, list) or not chunk:
                break
            repos.extend(_norm(r) for r in chunk if isinstance(r, dict))
            if len(chunk) < 100:
                break
        if repos:
            return repos
        errors.append("REST: empty answer")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        errors.append(f"REST: {str(exc)[:120]}")
    raise RuntimeError("could not refresh the inventory — " + "; ".join(errors))
