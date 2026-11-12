"""Build what the web app reads: one data bundle, optionally inlined into one file.

* ``app/data/bundle.js``   ``window.RECURSER_BUNDLE = {...}`` — works from ``file://`` and any static host.
* ``dist/recurser.html``   the whole app (markup, styles, scripts, data) in a single file you can
                           mail to yourself, drop on a phone, or open offline.
* ``dist/recurser.artifact.html``  the same page as a fragment (no doctype/head/body) for hosts
                           that wrap the page themselves.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

try:
    from . import address, inventory, schema, srs
except ImportError:  # pragma: no cover
    import address, inventory, schema, srs  # type: ignore

MAP_KEYS = (
    "title", "one_liner", "purpose", "domain", "status", "stack", "entry_points", "architecture", "data_flow",
    "key_functions", "infra", "decisions", "gotchas", "glossary", "relations", "directions", "confidence",
    "coverage_notes",
)
CARD_KEYS = (
    "id", "slug", "code", "category", "rank", "type", "level", "concept", "question", "answer", "explanation",
    "anchor", "example", "confusables", "cloze", "code_snippet", "expected_output", "steps", "evidence_quote",
    "tags", "source_refs", "stale", "see_also",
)


def build_bundle(edition: dict, *, generated_at: str | None = None) -> dict:
    owner = edition.get("owner", "")
    projects = []
    for p in edition["projects"]:
        item = {k: p.get(k) for k in MAP_KEYS if p.get(k) not in (None, "", [])}
        item.update(
            slug=p["slug"], code=p["code"], repo=p["repo"], visibility=p.get("visibility", "public"),
            fork=bool(p.get("fork")), pushed_at=p.get("pushed_at", ""), branch=p.get("branch", ""), commit=p.get("commit", ""),
            alts=p.get("alts") or [], aliases=p.get("aliases") or [], analyzed_at=p.get("analyzed_at", ""),
            card_count=p.get("card_count", 0), shelf=int(p["code"][0]),
        )
        if owner:
            item["github"] = f"https://github.com/{owner}/{p['repo']}"
        projects.append(item)
    cards = [{k: c[k] for k in CARD_KEYS if c.get(k) not in (None, "", [])} for c in edition["cards"]]
    inv = [
        {k: r.get(k) for k in ("name", "pushed_at", "visibility", "fork", "slug", "code") if r.get(k) not in (None, "")}
        for r in edition["inventory"]
    ]
    return {
        "schema": 1,
        "edition": edition.get("edition", "public"),
        "owner": owner,
        "as_of": edition.get("as_of", ""),
        "generated_at": generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "taxonomy": {
            "categories": [{"digit": d, "name": n, "mnemonic": m} for d, (n, m) in schema.CATEGORIES.items()],
            "shelves": [{"digit": d, "name": n, "mnemonic": m} for d, (n, m) in address.DOMAINS.items()],
            "types": schema.CARD_TYPES,
            "levels": {str(k): v for k, v in schema.LEVELS.items()},
        },
        "tuning": {
            "base_half_life_days": inventory.BASE_HALF_LIFE_DAYS,
            "mastery_gain": inventory.MASTERY_GAIN,
            "mature_days": srs.MATURE_DAYS,
            "again_delay_ms": srs.AGAIN_DELAY_MS,
            "min_ease": srs.MIN_EASE,
            "tiers": [{"max_days": d, "key": k, "label": l} for d, k, l in inventory.TIERS],
        },
        "projects": projects,
        "cards": cards,
        "inventory": inv,
    }


def js_literal(data) -> str:
    """JSON that is safe inside an inline <script> block: no ``<`` survives, so card text can neither close
    the script (``</script``), open an HTML comment (``<!--``) nor imitate the page's section markers."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def bundle_js(bundle: dict) -> str:
    return "window.RECURSER_BUNDLE = " + js_literal(bundle) + ";\n"


def write_bundle(root: str, bundle: dict) -> str:
    out_dir = os.path.join(root, "app", "data")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "bundle.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(bundle_js(bundle))
    return path


# --------------------------------------------------------------------------
# Single-file builds
# --------------------------------------------------------------------------
_LINK = re.compile(r'<link\s+rel="stylesheet"\s+href="([^"]+)"\s*/?>')
_SCRIPT = re.compile(r'<script\s+src="([^"]+)"\s*>\s*</script>')
_SECTION = "<!--{0}-->(.*?)<!--/{0}-->"


def _is_local(ref: str) -> bool:
    return not re.match(r"^(?:https?:)?//", ref)


def inline_app(root: str, bundle: dict) -> str:
    """index.html with every local stylesheet and script inlined, data included."""
    app_dir = os.path.join(root, "app")
    with open(os.path.join(app_dir, "index.html"), encoding="utf-8") as fh:
        html = fh.read()

    def read(rel: str) -> str:
        with open(os.path.join(app_dir, rel), encoding="utf-8") as fh:
            return fh.read()

    def link(m: re.Match) -> str:
        ref = m.group(1)
        return f"<style>\n{read(ref)}\n</style>" if _is_local(ref) else m.group(0)

    def script(m: re.Match) -> str:
        ref = m.group(1)
        if not _is_local(ref):
            return m.group(0)
        body = bundle_js(bundle) if ref.endswith("data/bundle.js") else read(ref)
        return "<script>\n" + body.replace("</script", "<\\/script") + "\n</script>"

    return _SCRIPT.sub(script, _LINK.sub(link, html))


def as_fragment(inlined_html: str) -> str:
    """Keep only what sits between the HEAD and BODY markers (for self-wrapping hosts)."""
    parts = []
    for name in ("HEAD", "BODY"):
        if inlined_html.count(f"<!--{name}-->") != 1 or inlined_html.count(f"<!--/{name}-->") != 1:
            raise ValueError(f"index.html must hold exactly one <!--{name}--> ... <!--/{name}--> pair")
        m = re.search(_SECTION.format(name), inlined_html, re.S)
        if not m:
            raise ValueError(f"index.html is missing the <!--{name}--> ... <!--/{name}--> markers")
        parts.append(m.group(1).strip())
    return "\n".join(parts) + "\n"


def write_single_file(root: str, bundle: dict, out_path: str, *, fragment: bool = False) -> str:
    html = inline_app(root, bundle)
    if fragment:
        html = as_fragment(html)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path
