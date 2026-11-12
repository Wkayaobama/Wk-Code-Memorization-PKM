"""Data tiers on disk, ingestion of analysis output, and the in-memory edition.

Layout::

    data/<tier>/projects.json    manifest: slug, repo, code, visibility, branch, aliases...
    data/<tier>/inventory.json   every repository of that tier with its last-push time
    data/<tier>/maps/<slug>.json one knowledge map per project
    data/<tier>/cards/<slug>.json the cards of that project

    data/private/unredacted/<slug>.json   full wording of a public deck that named a private project

Two tiers exist: ``public`` (derived from public repositories only; safe to
commit anywhere) and ``private`` (everything derived from private
repositories; git-ignored in the public repository).  An *edition* is the
merge of the tiers that are present.  A public deck may not name a private
repository: ingest redacts such mentions in the public tier and keeps the full
wording in the private tier, which the private edition prefers.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

try:  # package import or plain-script import
    from . import address, inventory, schema
except ImportError:  # pragma: no cover
    import address, inventory, schema  # type: ignore

TIERS = ("public", "private")


def head_commit(directory: str) -> str:
    """Commit currently checked out in a directory ('' when it is not a git checkout)."""
    import subprocess

    try:
        out = subprocess.run(["git", "-C", directory, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def _digest(pmap: dict, pcards: dict) -> str:
    import hashlib

    return hashlib.sha1(json.dumps([pmap, pcards], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _remove(path: str) -> bool:
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def tier_dir(root: str, tier: str) -> str:
    return os.path.join(root, "data", tier)


def present_tiers(root: str) -> list[str]:
    return [t for t in TIERS if os.path.exists(os.path.join(tier_dir(root, t), "projects.json"))]


# --------------------------------------------------------------------------
# Privacy gate: nothing about a private repository may enter the public tier
# --------------------------------------------------------------------------
# Only what was checked against the checkout is exempt from rewriting: a citation must keep matching the
# code.  Snippets, expected outputs and examples are composed by the card writer and are gated like prose.
CITATION_FIELDS = {"evidence_quote", "source", "source_refs", "path", "evidence"}
VERBATIM_FIELDS = CITATION_FIELDS   # kept as an alias for callers
MIN_NAME = 4                        # a shorter private name cannot be matched safely and is reported instead
REDACTED = "[private project]"   # a bracketed placeholder reads correctly after any article or adjective
UNREDACTED = "unredacted"  # data/private/unredacted/<slug>.json: full wording of a public deck that was redacted


def _name_pattern(names, strict: bool = False):
    """One expression for a list of names, tolerant of - _ and space between their parts.

    Loose (the gate): bounded by non-alphanumerics, so ``acme-load-bridge`` still gives ``acme-load`` away.
    Strict (the whitelist of names a public repository already spells out): additionally not glued to a
    longer hyphenated name, so ``acme-load-bridge`` is NOT a mention of ``acme-load``."""
    import re

    parts = []
    for name in sorted({n for n in names if n and len(n) >= MIN_NAME}, key=len, reverse=True):
        tokens = [re.escape(t) for t in re.split(r"[-_ ]+", name) if t]
        parts.append(r"[-_ ]?".join(tokens))
    if not parts:
        return None
    edge = r"A-Za-z0-9_\-" if strict else r"A-Za-z0-9"
    return re.compile(r"(?<![" + edge + r"])(?:" + "|".join(parts) + r")(?![" + edge + r"])", re.I)


def _norm_name(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch not in "-_ ")


SKIP_DIRS = {".git", "node_modules", "dist", "build", "__pycache__", ".venv", "venv", "target", ".next", ".cache"}


def public_vocabulary(private_names, repo_dirs, max_bytes: int = 2_000_000) -> set[str]:
    """Private names that a PUBLIC checkout already spells out itself, in a path or in a text file.

    Such a name is already public through that repository, so its own deck may keep using it
    (a public fork that carries a directory named like a later private project, for instance).
    """
    pattern = _name_pattern(private_names, strict=True)
    seen: set[str] = set()
    if pattern is None:
        return seen
    for base in repo_dirs:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            seen.update(_norm_name(m.group(0)) for m in pattern.finditer(os.path.relpath(dirpath, base)))
            for name in filenames:
                seen.update(_norm_name(m.group(0)) for m in pattern.finditer(name))
                path = os.path.join(dirpath, name)
                try:
                    if os.path.islink(path) or os.path.getsize(path) > max_bytes:
                        continue
                    with open(path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                if b"\0" in raw[:4096]:
                    continue
                seen.update(_norm_name(m.group(0)) for m in pattern.finditer(raw.decode("utf-8", "replace")))
    return {n for n in private_names if _norm_name(n) in seen}


def privacy_gate(obj, private_names, keep=(), exempt=None):
    """Return (clean copy, findings).  Relations to private projects are dropped; any other mention of a
    private repository name is replaced, unless the name is in ``keep`` (names the public repository
    already spells out itself).  The ``exempt`` fields are left alone; by default those are the citations,
    which quote the project's own code and must keep matching it."""
    exempt = CITATION_FIELDS if exempt is None else frozenset(exempt)
    relation_pattern = _name_pattern(private_names)
    kept = {_norm_name(k) for k in keep}
    plain = _name_pattern([n for n in private_names if _norm_name(n) not in kept])
    findings: list[str] = []
    if relation_pattern is None:
        return obj, findings
    text_pattern = plain

    def walk(node, key=""):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "relations" and isinstance(v, list):
                    kept_rel = [r for r in v if not (isinstance(r, dict) and relation_pattern.search(str(r.get("project", ""))))]
                    if len(kept_rel) != len(v):
                        findings.append(f"dropped {len(v) - len(kept_rel)} relation(s) to private projects")
                    out[k] = walk(kept_rel, k)
                elif k == "see_also" and isinstance(v, list):   # a link into a private deck leads nowhere in public
                    kept_links = [x for x in v if not (isinstance(x, str) and relation_pattern.search(x.split(":")[0]))]
                    if len(kept_links) != len(v):
                        findings.append(f"dropped {len(v) - len(kept_links)} link(s) to cards of private projects")
                    if kept_links:
                        out[k] = kept_links
                else:
                    out[k] = walk(v, k)
            return out
        if isinstance(node, list):
            return [walk(v, key) for v in node]
        if isinstance(node, str) and text_pattern is not None and key not in exempt and text_pattern.search(node):
            findings.append(f"redacted a private repository name in '{key}'")
            return text_pattern.sub(REDACTED, node)
        return node

    return walk(obj), findings


SCRUBBED = "[id]"


def scrub_terms(obj, terms, placeholder: str = SCRUBBED):
    """Return (clean copy, count).  Exact strings (a portal number, a cloud project id, a customer name,
    a revenue figure...) are replaced everywhere except in the citation fields, which must keep matching
    the code.  The list is supplied by whoever runs the ingest; an entry is either a string (replaced by
    ``[id]``) or ``{"term": "...", "as": "[account]"}`` for another placeholder ("" removes the text)."""
    import re

    table: dict[str, str] = {}
    for entry in terms or []:
        term, repl = (entry.get("term"), entry.get("as", placeholder)) if isinstance(entry, dict) else (entry, placeholder)
        if term is not None and str(term).strip():
            table[str(term)] = str(repl)
    if not table:
        return obj, 0
    # A number is bounded by non-digits (it also hides inside names such as "p<portal>_object");
    # anything else is bounded by non-alphanumerics on the sides where it starts or ends with one.
    def bounded(term: str) -> str:
        if term.isdigit():
            return r"(?<![0-9])" + re.escape(term) + r"(?![0-9])"
        left = r"(?<![A-Za-z0-9])" if term[0].isalnum() else ""
        right = r"(?![A-Za-z0-9])" if term[-1].isalnum() else ""
        return left + re.escape(term) + right

    pattern = re.compile("|".join(bounded(t) for t in sorted(table, key=len, reverse=True)))
    count = 0

    def swap(match):
        return table[match.group(0)]

    def walk(node, key=""):
        nonlocal count
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            if key == "aliases":   # a nickname built on an identifier is dropped, not rewritten
                kept = [v for v in node if not (isinstance(v, str) and pattern.search(v))]
                count += len(node) - len(kept)
                return kept
            return [walk(v, key) for v in node]
        if isinstance(node, str) and key not in CITATION_FIELDS:
            new, n = pattern.subn(swap, node)
            count += n
            return re.sub(r" {2,}", " ", new).strip() if n else new
        return node

    return walk(obj), count


def promote_bracket_paths(pmap: dict, repo_dirs: list[str]) -> int:
    """Maps written while dot-directories could not be cited carry the true location in brackets at the
    start of a text field and '.' as the path.  Move the location back into the path field."""
    import re

    lead = re.compile(r"^\[([^\]\s]+)\]\s*")
    moved = 0
    for kind, text_keys in (("architecture", ("responsibility",)), ("key_functions", ("does", "signature")), ("entry_points", ("what",)),
                            ("infra", ("detail",)), ("gotchas", ("issue", "consequence", "fix_or_guard"))):
        for item in pmap.get(kind) or []:
            if item.get("path") not in (".", "./", "", None):
                continue
            for tk in text_keys:
                m = lead.match(str(item.get(tk) or ""))
                if m and schema.resolve_source(m.group(1), repo_dirs)[0]:
                    item["path"] = m.group(1)
                    item[tk] = item[tk][m.end():]
                    moved += 1
                    break
    return moved


# --------------------------------------------------------------------------
# Ingest: analysis output -> tiers
# --------------------------------------------------------------------------
def _resolve_refs(card: dict, dirs: list[tuple[str, str, str]]) -> list[dict]:
    """Attach the branch and commit that actually hold each cited path."""
    refs = []
    for ref in card.get("source", []):
        parts = schema.split_source(ref)
        if not parts:
            continue
        rel, a, b = parts
        branch, commit = (dirs[0][1], dirs[0][2]) if dirs else ("", "")
        for d, br, sha in dirs:
            if os.path.exists(os.path.join(d, rel)):
                branch, commit = br, sha
                break
        item = {"path": rel, "branch": branch}
        if commit:
            item["commit"] = commit
        if a is not None:
            item["line"] = a
            if b and b != a:
                item["end"] = b
        refs.append(item)
    return refs


def ingest(
    root: str,
    *,
    manifest_path: str,
    maps_dir: str,
    cards_dir: str,
    codes: dict[str, str],
    snapshot_path: str | None = None,
    aliases: dict[str, list[str]] | None = None,
    as_of: str | None = None,
    owner: str = "",
    scrub: dict | None = None,
    log=print,
) -> dict:
    """Validate analysis output against the checkouts and file it into the tiers.

    A project is filed only when its map AND its cards pass every check (shape, grounding against an
    existing checkout, safety), before and after the privacy rewriting.  Everything is planned first and
    written last: a run either files its projects or leaves the tiers as they were.  Projects that are not
    part of this run stay filed; a project whose visibility changed leaves its old tier.
    Only a repository whose visibility is exactly "public" is public.
    """
    manifest = _read(manifest_path)
    projects = manifest["projects"] if isinstance(manifest, dict) else manifest
    snapshot = inventory.load_snapshot(snapshot_path) if snapshot_path else []
    aliases = aliases or {}
    private = [p for p in projects if not inventory.is_public(p.get("visibility"))]
    # Repository names, slugs, and any product name the manifest marks as private ("private_terms").
    # Nicknames are left out on purpose: they are generic words ("the loader") that a public deck may use.
    private_names = sorted({r["name"] for r in snapshot if not inventory.is_public(r.get("visibility"))}
                           | {p["repo"] for p in private} | {p["slug"] for p in private}
                           | {t for p in private for t in (p.get("private_terms") or [])})
    as_of = as_of or (manifest.get("as_of") if isinstance(manifest, dict) else None) or date.today().isoformat()
    owner = owner or (manifest.get("owner") if isinstance(manifest, dict) else "") or ""
    report = {"filed": [], "skipped": [], "moved": [], "unmatched_private_names": sorted(n for n in private_names if len(n) < MIN_NAME)}
    planned: list[dict] = []     # {"tier", "slug", "entry", "map", "cards", "overlay"}

    def skip(slug, why, errors=None):
        report["skipped"].append({"slug": slug, "why": why, **({"errors": errors[:8]} if errors else {})})

    for p in projects:
        slug = p["slug"]
        map_path = os.path.join(maps_dir, f"{slug}.json")
        cards_path = os.path.join(cards_dir, f"{slug}.json")
        if not (os.path.exists(map_path) and os.path.exists(cards_path)):
            skip(slug, "map or cards file missing")
            continue
        if slug not in codes:
            skip(slug, "no project code assigned")
            continue
        listed = [p.get("dir") or ""] + [a.get("dir") or "" for a in p.get("alts", [])]
        missing = [d for d in listed if not os.path.isdir(d)]
        if missing:   # no checkout, no grounding: never file a deck that could not be checked
            skip(slug, "checkout not found: " + ", ".join(repr(d) for d in missing))
            continue
        dirs = [(p["dir"], p.get("branch", ""), head_commit(p["dir"]))]
        dirs += [(a["dir"], a.get("branch", ""), head_commit(a["dir"])) for a in p.get("alts", [])]
        repo_dirs = [d for d, _, _ in dirs]
        pmap, pcards = _read(map_path), _read(cards_path)
        if not (isinstance(pmap, dict) and isinstance(pcards, dict)):
            skip(slug, "map and cards must be JSON objects")
            continue
        promote_bracket_paths(pmap, repo_dirs)
        m_rep = schema.validate_map(pmap, repo_dirs)
        c_rep = schema.validate_cards(pcards, repo_dirs)
        if not (m_rep.ok and c_rep.ok):
            skip(slug, "validation failed", m_rep.errors + c_rep.errors)
            continue
        tier = "public" if inventory.is_public(p.get("visibility")) else "private"
        for card in pcards["cards"]:
            card["source_refs"] = _resolve_refs(card, dirs)
        redactions: list[str] = []
        pmap["slug"] = slug
        pcards["slug"] = slug
        merged_aliases = list(dict.fromkeys([*(aliases.get(slug) or []), *(pmap.get("aliases") or [])]))
        notes = [a.get("note", "") for a in p.get("alts", [])]
        scrubbed = 0
        if scrub and scrub.get("all"):     # identifiers that travel nowhere, private tier included
            pmap, n_m = scrub_terms(pmap, scrub["all"])
            pcards, n_c = scrub_terms(pcards, scrub["all"])
            wrapped, n_a = scrub_terms({"aliases": merged_aliases, "notes": notes}, scrub["all"])
            merged_aliases, notes, scrubbed = wrapped["aliases"], wrapped["notes"], n_m + n_c + n_a
        overlay = None
        if tier == "public":
            full = {"map": pmap, "cards": pcards, "aliases": merged_aliases}
            if scrub and scrub.get("public"):   # identifiers the owner may keep but a public deck may not repeat
                pmap, n_m = scrub_terms(pmap, scrub["public"])
                pcards, n_c = scrub_terms(pcards, scrub["public"])
                wrapped, n_a = scrub_terms({"aliases": merged_aliases, "notes": notes}, scrub["public"])
                merged_aliases, notes = wrapped["aliases"], wrapped["notes"]
                if n_m + n_c + n_a:
                    redactions.append(f"scrubbed {n_m + n_c + n_a} identifier(s)")
            own = public_vocabulary(private_names, repo_dirs)   # names this public repository already spells out
            pmap, found_m = privacy_gate(pmap, private_names, own)
            pcards, found_c = privacy_gate(pcards, private_names, own)
            merged_aliases, found_a = privacy_gate(merged_aliases, private_names, own)
            notes, found_n = privacy_gate(notes, private_names, own)
            redactions += found_m + found_c + found_a + found_n
            if redactions:
                # The rewritten copy must still honour the contract (a placeholder can lengthen a field).
                m2, c2 = schema.validate_map(pmap), schema.validate_cards(pcards)
                if not (m2.ok and c2.ok):
                    skip(slug, "the redacted public copy no longer fits the contract: shorten these fields in the source deck", m2.errors + c2.errors)
                    continue
                overlay = {**full, "public_sha": _digest(pmap, pcards)}   # full wording, for the owner's edition only
        entry = {
            "slug": slug,
            "repo": p["repo"],
            "code": str(codes[slug]),
            "visibility": tier,
            "fork": bool(p.get("fork")),
            "pushed_at": p.get("pushed_at", ""),
            "branch": p.get("branch", ""),
            "commit": dirs[0][2],
            "alts": [{"branch": a.get("branch", ""), "note": note, "commit": sha}
                     for a, note, (_, _, sha) in zip(p.get("alts", []), notes, dirs[1:])],
            "aliases": merged_aliases,
            "analyzed_at": as_of,
        }
        planned.append({"tier": tier, "slug": slug, "entry": entry, "map": pmap, "cards": pcards, "overlay": overlay,
                        "line": {"slug": slug, "tier": tier, "cards": len(pcards["cards"]), "warnings": len(c_rep.warnings),
                                 "redactions": len(redactions), "scrubbed": scrubbed}})

    # ---- plan the manifests: what is already filed stays, except where this run replaces or moves it
    filed_now = {item["slug"]: item["tier"] for item in planned}
    manifests: dict[str, list[dict]] = {}
    for tier in TIERS:
        current = _read(os.path.join(tier_dir(root, tier), "projects.json"), {"projects": []}).get("projects", [])
        kept = [e for e in current if e["slug"] not in filed_now]
        for e in current:
            if e["slug"] in filed_now and filed_now[e["slug"]] != tier:
                report["moved"].append({"slug": e["slug"], "from": tier, "to": filed_now[e["slug"]]})
        manifests[tier] = sorted(kept + [item["entry"] for item in planned if item["tier"] == tier], key=lambda e: e["code"])
    address.check_registry([e for t in TIERS for e in manifests[t]])   # raises before anything is written

    # ---- write
    for item in planned:
        slug, tier = item["slug"], item["tier"]
        other = "private" if tier == "public" else "public"
        _write(os.path.join(tier_dir(root, tier), "maps", f"{slug}.json"), item["map"])
        _write(os.path.join(tier_dir(root, tier), "cards", f"{slug}.json"), item["cards"])
        for kind in ("maps", "cards"):   # a project lives in exactly one tier
            _remove(os.path.join(tier_dir(root, other), kind, f"{slug}.json"))
        overlay_path = os.path.join(tier_dir(root, "private"), UNREDACTED, f"{slug}.json")
        if item["overlay"]:
            _write(overlay_path, item["overlay"])
        else:
            _remove(overlay_path)
        report["filed"].append(item["line"])
        line = item["line"]
        log(f"filed {slug:<32} -> {tier:<7} {line['cards']} cards" + (f"  ({line['redactions']} private mentions removed)" if line["redactions"] else "")
            + (f"  ({line['scrubbed']} identifiers scrubbed)" if line["scrubbed"] else ""))
    for tier in TIERS:
        path = os.path.join(tier_dir(root, tier), "projects.json")
        if manifests[tier] or os.path.exists(path):
            _write(path, {"as_of": as_of, "owner": owner, "projects": manifests[tier]})
        stale_path = os.path.join(tier_dir(root, tier), "stale.json")
        stale = _read(stale_path)
        if stale:   # a refreshed deck starts clean; a project that left the tier takes its marks with it
            here = {e["slug"] for e in manifests[tier]}
            _write(stale_path, {k: v for k, v in stale.items() if k in here and k not in filed_now})

    if snapshot_path:
        for tier in TIERS:
            subset = [r for r in snapshot if inventory.is_public(r.get("visibility")) == (tier == "public")]
            path = os.path.join(tier_dir(root, tier), "inventory.json")
            if subset or os.path.exists(path):
                _write(path, {"as_of": as_of, "owner": owner, "repos": subset})
    for item in report["moved"]:
        log(f"moved {item['slug']} from the {item['from']} tier to the {item['to']} tier")
    if report["unmatched_private_names"]:
        log("private names too short to match safely, check the public decks by hand: " + ", ".join(report["unmatched_private_names"]))
    return report


# --------------------------------------------------------------------------
# Edition: tiers -> one in-memory structure
# --------------------------------------------------------------------------
def load_edition(root: str, tiers: list[str] | None = None, today: date | None = None) -> dict:
    """Merge the requested tiers into one validated structure used by every exporter."""
    tiers = tiers or present_tiers(root)
    today = today or datetime.now(timezone.utc).date()
    projects, cards, repos = [], [], []
    owner, as_of = "", ""
    warnings: list[str] = []
    unknown = [t for t in tiers if t not in TIERS]
    if unknown:
        raise ValueError(f"unknown tier(s) {unknown}: the tiers are {list(TIERS)}")
    for tier in tiers:
        base = tier_dir(root, tier)
        manifest = _read(os.path.join(base, "projects.json"), {"projects": []})
        owner = owner or manifest.get("owner", "")
        as_of = max(as_of, manifest.get("as_of", ""))
        inv = _read(os.path.join(base, "inventory.json"), {"repos": []})
        repos.extend(inv.get("repos", []))
        stale = _read(os.path.join(base, "stale.json"), {})
        for entry in manifest.get("projects", []):
            slug = entry["slug"]
            pmap = _read(os.path.join(base, "maps", f"{slug}.json"))
            pcards = _read(os.path.join(base, "cards", f"{slug}.json"))
            if pmap is None or pcards is None:
                raise FileNotFoundError(f"tier {tier}: map or cards missing for {slug}")
            full = _read(os.path.join(tier_dir(root, "private"), UNREDACTED, f"{slug}.json")) if tier == "public" and "private" in tiers else None
            if full and full.get("public_sha") == _digest(pmap, pcards):
                pmap, pcards, entry = full["map"], full["cards"], {**entry, "aliases": full.get("aliases") or entry.get("aliases")}
            elif full:   # the public deck was edited since: the edited deck wins over the stale full wording
                warnings.append(f"{slug}: the public deck changed after its last ingest; its unredacted copy is ignored until the next ingest")
            m_rep, c_rep = schema.validate_map(pmap), schema.validate_cards(pcards)
            if not (m_rep.ok and c_rep.ok):
                raise ValueError(f"tier {tier}: {slug} is invalid — " + "; ".join((m_rep.errors + c_rep.errors)[:4]))
            project = {**pmap, **entry, "tier": tier, "aliases": entry.get("aliases") or pmap.get("aliases") or []}
            project["card_count"] = len(pcards["cards"])
            projects.append(project)
            for c in pcards["cards"]:
                cards.append(
                    {
                        **c,
                        "id": address.card_id(slug, c["category"], c["rank"]),
                        "code": address.card_code(entry["code"], c["category"], c["rank"]),
                        "slug": slug,
                        **({"stale": True} if schema.card_key(c) in (stale.get(slug) or []) else {}),
                    }
                )
    address.check_registry(projects)
    projects.sort(key=lambda p: p["code"])
    cards.sort(key=lambda c: c["code"])
    known = {c["id"] for c in cards}
    for c in cards:   # a cross-link only survives when its target is part of this edition
        if "see_also" in c:
            links = [x for x in c["see_also"] if x in known and x != c["id"]]
            if links:
                c["see_also"] = links
            else:
                del c["see_also"]

    by_repo = {p["repo"].lower(): p for p in projects}
    names = {r["name"].lower() for r in repos}
    for p in projects:  # a project whose repository is missing from the snapshot still belongs in the inventory
        if p["repo"].lower() not in names:
            repos.append({"name": p["repo"], "pushed_at": p.get("pushed_at", ""), "visibility": p.get("visibility", "public"), "fork": p.get("fork", False)})
    analyzed = inventory.analyze(repos, today)
    for r in analyzed:
        p = by_repo.get(r["name"].lower())
        r["slug"] = p["slug"] if p else None
        r["code"] = p["code"] if p else None
    return {
        "owner": owner,
        "as_of": as_of or today.isoformat(),
        "today": today.isoformat(),
        "tiers": tiers,
        "edition": "public" if list(tiers) == ["public"] and all(inventory.is_public(p.get("visibility")) for p in projects) else "private",
        "warnings": warnings,
        "projects": projects,
        "cards": cards,
        "inventory": analyzed,
    }


# What identifies an item itself and is therefore never rewritten.  A branch name is not in the list:
# a branch can be named after another project, and the commit is enough to link to the code.
IDENTITY_KEYS = ("slug", "repo", "code", "visibility", "fork", "pushed_at", "commit", "tier", "analyzed_at", "card_count", "id")


def resolve_projects(edition: dict, names) -> list[str]:
    """Slugs for a list of slugs, repository names or two-digit codes.  Exact matches only: a page for a
    chosen audience must never include a project by a lucky guess.  Raises KeyError naming what is unknown."""
    by_key: dict[str, str] = {}
    for p in edition["projects"]:
        for key in (p["slug"], p.get("repo") or "", p["code"]):
            by_key[str(key).strip().lower()] = p["slug"]
    slugs, unknown = [], []
    for name in names:
        slug = by_key.get(str(name).strip().lower())
        if slug is None:
            unknown.append(str(name))
        elif slug not in slugs:
            slugs.append(slug)
    if unknown:
        raise KeyError(", ".join(unknown))
    return slugs


def restrict(edition: dict, slugs) -> dict:
    """The same edition for one audience: only the chosen projects.

    The other projects disappear with their cards and their inventory rows, links to their cards are
    dropped, and wherever the remaining text names a private repository that is not part of the
    selection, the name is replaced, in quoted code, paths and branch names too.  Names too short to
    match safely are listed under ``unmatched_private_names`` so the caller can refuse to go on.
    """
    wanted = set(slugs)
    unknown = wanted - {p["slug"] for p in edition["projects"]}
    if unknown:
        raise KeyError(", ".join(sorted(unknown)))
    hidden = sorted({name for p in edition["projects"] if p["slug"] not in wanted and not inventory.is_public(p.get("visibility"))
                     for name in (p["slug"], p.get("repo") or "")}
                    | {r["name"] for r in edition["inventory"] if r.get("slug") not in wanted and not inventory.is_public(r.get("visibility"))})

    def gated(obj: dict) -> dict:
        clean, _ = privacy_gate(obj, hidden, exempt=())                  # an audience page is not checked against the code
        clean.update({k: obj[k] for k in IDENTITY_KEYS if k in obj})     # never rewrite what identifies the item itself
        return clean

    projects = [gated(p) for p in edition["projects"] if p["slug"] in wanted]
    cards = [gated(c) for c in edition["cards"] if c["slug"] in wanted]
    known = {c["id"] for c in cards}
    for c in cards:
        links = [x for x in c.get("see_also") or [] if x in known and x != c["id"]]
        if links:
            c["see_also"] = links
        else:
            c.pop("see_also", None)
    return {**edition, "projects": projects, "cards": cards,
            "inventory": [r for r in edition["inventory"] if r.get("slug") in wanted],
            "audience": sorted(wanted),
            "unmatched_private_names": sorted(n for n in hidden if n and len(n) < MIN_NAME)}


def find_stale(root: str, sources_root: str, tiers: list[str] | None = None) -> dict:
    """Re-check every card against a fresh checkout: a moved path or a vanished quote makes the card stale.

    ``sources_root/<slug>`` is the checkout of a project (``<slug>@<anything>`` siblings are alternate
    lines of work).  A project without a checkout is reported as unchecked, never as stale.
    """
    report = {"stale": {}, "confirmed": 0, "unchecked": []}
    for tier in tiers or present_tiers(root):
        base = tier_dir(root, tier)
        manifest = _read(os.path.join(base, "projects.json"), {"projects": []})
        for entry in manifest.get("projects", []):
            slug = entry["slug"]
            primary = os.path.join(sources_root, slug)
            if not os.path.isdir(primary):
                report["unchecked"].append(slug)
                continue
            dirs = [primary] + sorted(
                os.path.join(sources_root, d) for d in os.listdir(sources_root)
                if d.startswith(slug + "@") and os.path.isdir(os.path.join(sources_root, d))
            )
            pcards = _read(os.path.join(base, "cards", f"{slug}.json"), {"cards": []})
            for card in pcards.get("cards", []):
                files, missing = [], False
                for ref in card.get("source", []):
                    full, _why = schema.resolve_source(ref, dirs)
                    if full:
                        files.append(full)
                    else:
                        missing = True
                if missing or not schema.quote_found(card.get("evidence_quote", ""), files):
                    report["stale"].setdefault(slug, []).append(schema.card_key(card))
                else:
                    report["confirmed"] += 1
    return report


def write_stale(root: str, report: dict, tiers: list[str] | None = None) -> None:
    """Record the stale addresses per tier; the next build marks those cards.  A project that could not be
    checked this time (no checkout) keeps the marks it had."""
    unchecked = set(report.get("unchecked") or [])
    for tier in tiers or present_tiers(root):
        base = tier_dir(root, tier)
        manifest = _read(os.path.join(base, "projects.json"), {"projects": []})
        slugs = {e["slug"] for e in manifest.get("projects", [])}
        previous = _read(os.path.join(base, "stale.json"), {}) or {}
        merged = {slug: keys for slug, keys in previous.items() if slug in slugs and slug in unchecked}
        merged.update({slug: keys for slug, keys in report["stale"].items() if slug in slugs})
        _write(os.path.join(base, "stale.json"), dict(sorted(merged.items())))


def edition_stats(edition: dict) -> dict:
    cards = edition["cards"]
    by = lambda key: {k: sum(1 for c in cards if c[key] == k) for k in sorted({c[key] for c in cards})}  # noqa: E731
    return {
        "projects": len(edition["projects"]),
        "cards": len(cards),
        "repositories": len(edition["inventory"]),
        "by_type": by("type"),
        "by_level": by("level"),
        "by_category": by("category"),
        "private_projects": sum(1 for p in edition["projects"] if p.get("visibility") == "private"),
    }
