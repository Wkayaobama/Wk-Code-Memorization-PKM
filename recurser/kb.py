"""Knowledge-base export — one Markdown page per project for an Obsidian or TiddlyWiki vault.

The page is the project's documentation (the knowledge map) with its cards
inline as open ``Q:`` / ``A:`` blocks.  Every card ends with its stable id, so a
vault plugin or a later sync can match a block to its card without guessing.

    kb/00-index.md          index of indexes: every project with its address and last push
    kb/<PP>-<slug>.md       purpose, flow, architecture, key functions, decisions, traps, cards, my index
"""
from __future__ import annotations

import os

try:
    from . import schema
except ImportError:  # pragma: no cover
    import schema  # type: ignore

INDEX_SLOTS = [
    "▢ **trap I hit** → symptom · cause · fix",
    "▢ **command I forgot** → the flag, the order",
    "▢ **numbers to re-derive** → how, in one line",
    "▢ **where to look** → file · grader line · fixture",
    "▢ **the sentence** — if I could carry one line into the exam for this project, it is: ________________",
]


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _fence(text: str) -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}\n{text.rstrip()}\n{ticks}"


def _facts(items, title_key, body_keys, path_key=None, labels=None) -> list[str]:
    out, labels = [], labels or {}
    for it in items or []:
        line = f"- **{_one_line(it.get(title_key))}**"
        if path_key and it.get(path_key):
            line += f" (`{it[path_key]}`)"
        out.append(line)
        for k in body_keys:
            if it.get(k):
                out.append(f"  - {labels.get(k, '')}{_one_line(it[k])}")
    return out


def card_block(card: dict, index: dict | None = None) -> list[str]:
    lines = [f"**Q ({card['code'][:2]}·{card['code'][2:]}, {card['type']}, L{card['level']}):** {_one_line(card.get('question'))}"]
    if card.get("code_snippet"):
        lines += ["", _fence(card["code_snippet"]), ""]
    if card.get("cloze"):
        lines.append("> " + schema.CLOZE_RE.sub("[...]", _one_line(card["cloze"])))
    if card["type"] == "trace" and card.get("steps"):
        lines.append("**A:**")
        lines += [f"{i}. {_one_line(s)}" for i, s in enumerate(card["steps"], start=1)]
    else:
        lines.append(f"**A:** {_one_line(card.get('answer'))}")
    if card.get("expected_output"):
        lines += ["", _fence(card["expected_output"]), ""]
    for key, label in (("confusables", "Do not confuse with"), ("explanation", "Why"), ("anchor", "Anchor")):
        if card.get(key):
            lines.append(f"- *{label}:* {_one_line(card[key])}")
    links = [(index or {}).get(x) for x in card.get("see_also") or []]
    if any(links):
        lines.append("- *See also:* " + "; ".join(f"{t['code'][:2]}·{t['code'][2:]} {_one_line(t.get('concept'))} ({t['slug']})" for t in links if t))
    refs = card.get("source_refs") or []
    where = ", ".join(r["path"] + (f":{r['line']}" if r.get("line") else "") for r in refs) or ", ".join(card.get("source") or [])
    lines.append(f"- *Source:* `{where}`  <!-- recurser:{card['id']} -->")
    return lines


def project_page(project: dict, cards: list[dict], gaps: dict | None = None, index: dict | None = None) -> str:
    p, gaps = project, gaps or {}
    out = [
        "---",
        f"title: \"{_one_line(p.get('title') or p['slug'])}\"",
        f"recurser_code: \"{p['code']}\"",
        f"repository: {p['repo']}",
        f"branch: {p.get('branch', '')}",
        f"commit: {p.get('commit', '')}",
        f"last_push: {p.get('pushed_at', '')[:10]}",
        f"analysed: {p.get('analyzed_at', '')}",
        f"status: {p.get('status', '')}",
        "tags: [recurser, project]",
        "---",
        "",
        f"# {p['code']} · {_one_line(p.get('title') or p['slug'])}",
        "",
        f"> {_one_line(p.get('one_liner'))}",
        "",
        "## Purpose",
        "",
        _one_line(p.get("purpose")),
        "",
    ]
    if p.get("data_flow"):
        out += ["## Data flow", ""] + [f"{i}. {_one_line(str(s).lstrip('0123456789.) '))}" for i, s in enumerate(p["data_flow"], start=1)] + [""]
    sections = [
        ("Architecture", _facts(p.get("architecture"), "component", ["responsibility"], "path")),
        ("Key functions", _facts(p.get("key_functions"), "name", ["signature", "does", "expected_output"], "path", {"expected_output": "Returns: "})),
        ("Entry points", _facts(p.get("entry_points"), "command", ["what"], "path")),
        ("Infrastructure", _facts(p.get("infra"), "topic", ["detail"], "path")),
        ("Decisions", _facts(p.get("decisions"), "decision", ["why", "alternative_rejected"], "evidence", {"why": "Why: ", "alternative_rejected": "Rejected: "})),
        ("Known traps", _facts(p.get("gotchas"), "issue", ["consequence", "fix_or_guard"], "path", {"fix_or_guard": "Guard: "})),
        ("Glossary", _facts(p.get("glossary"), "term", ["meaning"])),
        ("Related projects", _facts(p.get("relations"), "project", ["relation"])),
    ]
    for title, lines in sections:
        if lines:
            out += [f"## {title}", ""] + lines + [""]
    if p.get("directions"):
        out += ["## Next directions", ""] + [f"- {_one_line(d)}" for d in p["directions"]] + [""]
    out += ["## Cards", "", "Cover the answers and read the questions cold. Addresses never change.", ""]
    for card in sorted(cards, key=lambda c: c["code"]):
        out += card_block(card, index)
        gap = gaps.get(card["id"])
        if gap and gap.get("text"):
            out.append(f"- *Last gap:* {_one_line(gap['text'])}")
        out.append("")
    out += ["## My index", "", "Fill from your attempt (allowed in the exam).", ""] + [f"- {slot}" for slot in INDEX_SLOTS] + [""]
    if p.get("coverage_notes"):
        out += ["## How this page was built", "", _one_line(p["coverage_notes"]), ""]
    return "\n".join(out)


def export_kb(edition: dict, out_dir: str, *, progress: dict | None = None) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    gaps = (progress or {}).get("gaps") or {}
    by_slug: dict[str, list[dict]] = {}
    by_id = {c["id"]: c for c in edition["cards"]}
    for c in edition["cards"]:
        by_slug.setdefault(c["slug"], []).append(c)
    paths = []
    index = ["# Recurser — index of project pages", "", f"Inventory as of {edition.get('as_of', '')}. Address = shelf, slot.", "",
             "| Address | Project | Repository | Last push | Cards |", "|---|---|---|---|---|"]
    for p in edition["projects"]:
        name = f"{p['code']}-{p['slug']}.md"
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(project_page(p, by_slug.get(p["slug"], []), gaps, by_id))
        paths.append(path)
        index.append(f"| {p['code']} | [[{name[:-3]}\\|{_one_line(p.get('title') or p['slug']).replace('|', '/')}]] | {p['repo']} | {p.get('pushed_at', '')[:10]} | {len(by_slug.get(p['slug'], []))} |")
    path = os.path.join(out_dir, "00-index.md")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(index) + "\n")
    return [path] + paths
