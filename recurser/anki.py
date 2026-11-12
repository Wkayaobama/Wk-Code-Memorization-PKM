"""Anki exports — always last, always a projection of the card files.

Anki is the drill engine, not the source of truth: edits made in Anki are
overwritten by the next import.  Three routes, by increasing comfort:

1. ``export_for_anki/<PP>-<slug>.csv``  the owner's classic shape: semicolon separated, UTF-8 with BOM,
   no header, ``Code;Concept;Anchor;Answer;Example;Confusables``.
2. ``recurser-cards.txt`` / ``recurser-steps.txt``  tab separated with Anki file headers (guid, deck and
   tags columns), so a re-import updates notes in place instead of duplicating them.
3. ``recurser.apkg``  (needs ``pip install genanki``) carries the two note types, their conditional
   card templates and the styling, so nothing has to be recreated by hand.

Two note types, versioned by name (a schema change means ``v2``, never an edited ``v1``):

* ``Recurser Card v1``  one note per address; up to four cards per note, each wrapped in its field so a
  blank field yields no card: Main, Confusable, Anchor, Address (reverse, keystones only).
* ``Recurser Step v1``  one note per step of an ordered chain: two steps of context, recite the next.
"""
from __future__ import annotations

import csv
import hashlib
import html
import os
import time
import zlib

try:
    from . import schema
except ImportError:  # pragma: no cover
    import schema  # type: ignore

CARD_MODEL = "Recurser Card v1"
STEP_MODEL = "Recurser Step v1"
CARD_MODEL_ID = 1794480001
STEP_MODEL_ID = 1794480002
CARD_FIELDS = ["ID", "Code", "Project", "Category", "Type", "Level", "Concept", "Question", "Snippet", "ClozeFront",
               "Answer", "ClozeBack", "ExpectedOutput", "Explanation", "Anchor", "Example", "Confusables", "Source",
               "Evidence", "Permalink", "Keystone"]
STEP_FIELDS = ["Title", "Sequence", "Context", "Line", "Prompt", "Source", "ID"]

CSS = """
.card { font-family: "IBM Plex Sans", -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 19px; line-height: 1.45;
        text-align: left; color: #121924; background: #ffffff; max-width: 40em; margin: 0 auto; padding: 12px; }
.head { font: 500 13px/1.3 "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace; color: #5d697b; margin-bottom: 14px; }
.q { font-weight: 500; font-size: 21px; }
pre { background: #edf0f5; border-radius: 8px; padding: 10px 12px; font: 400 14px/1.5 "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace;
      white-space: pre-wrap; overflow-wrap: anywhere; }
.a { font-size: 19px; }
.fill { font-weight: 700; color: #1c5cab; }
.lab { font: 600 12px/1.3 -apple-system, "Segoe UI", sans-serif; letter-spacing: .05em; text-transform: uppercase; color: #5d697b; margin-top: 14px; }
.foot { font: 400 12px/1.4 "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace; color: #5d697b; margin-top: 18px; }
.nightMode.card, .night_mode.card { color: #e8ecf1; background: #161b23; }
.nightMode pre, .night_mode pre { background: #1a212c; }
.nightMode .fill, .night_mode .fill { color: #6da7ec; }
"""

_HEAD = '<div class="head">{{Project}} · {{Code}} · {{Category}}</div>'
_FOOT = '<div class="foot">[{{Project}} / {{Source}} · {{ID}}] {{#Permalink}}<a href="{{Permalink}}">open the source</a>{{/Permalink}}</div>'
CARD_TEMPLATES = [
    {
        "name": "Main",
        "qfmt": "{{#Question}}" + _HEAD + '<div class="q">{{Question}}</div>{{#Snippet}}<pre>{{Snippet}}</pre>{{/Snippet}}'
                "{{#ClozeFront}}<div class=\"a\">{{ClozeFront}}</div>{{/ClozeFront}}{{/Question}}",
        "afmt": _HEAD + '<div class="q">{{Question}}</div>{{#Snippet}}<pre>{{Snippet}}</pre>{{/Snippet}}<hr id=answer>'
                "{{#ExpectedOutput}}<pre>{{ExpectedOutput}}</pre>{{/ExpectedOutput}}"
                '{{#ClozeBack}}<div class="a">{{ClozeBack}}</div>{{/ClozeBack}}<div class="a">{{Answer}}</div>'
                '{{#Confusables}}<div class="lab">Do not confuse with</div><div>{{Confusables}}</div>{{/Confusables}}'
                '{{#Explanation}}<div class="lab">Why</div><div>{{Explanation}}</div>{{/Explanation}}'
                '{{#Example}}<div class="lab">Example</div><pre>{{Example}}</pre>{{/Example}}'
                '{{#Anchor}}<div class="lab">Anchor</div><div>{{Anchor}}</div>{{/Anchor}}'
                '{{#Evidence}}<div class="lab">Source</div><pre>{{Evidence}}</pre>{{/Evidence}}' + _FOOT,
    },
    {
        "name": "Confusable",
        "qfmt": "{{#Confusables}}" + _HEAD + '<div class="q">{{Project}}: what must “{{Concept}}” not be confused with, and what is the ONE difference?</div>{{/Confusables}}',
        "afmt": "{{FrontSide}}<hr id=answer><div class=\"a\">{{Confusables}}</div>" + _FOOT,
    },
    {
        "name": "Anchor",
        "qfmt": "{{#Anchor}}" + _HEAD + '<div class="q">Anchor scene at {{Code}} — {{Concept}}?</div>{{/Anchor}}',
        "afmt": "{{FrontSide}}<hr id=answer><div class=\"a\">{{Anchor}}</div>" + _FOOT,
    },
    {
        "name": "Address",
        "qfmt": '{{#Keystone}}<div class="head">{{Project}}</div><div class="q">Address of “{{Concept}}” in {{Project}}?</div>{{/Keystone}}',
        "afmt": "{{FrontSide}}<hr id=answer><div class=\"a\"><span class=\"fill\">{{Code}}</span> — {{Category}}</div>" + _FOOT,
    },
]
STEP_TEMPLATES = [
    {
        "name": "Next step",
        "qfmt": '<div class="head">{{Title}} · step {{Sequence}}</div><div class="a">{{Context}}</div><div class="q">{{Prompt}}</div>',
        "afmt": '<div class="head">{{Title}} · step {{Sequence}}</div><div class="a">{{Context}}</div><hr id=answer>'
                '<div class="a fill">{{Line}}</div><div class="foot">[{{Source}} · {{ID}}]</div>',
    }
]


def esc(value) -> str:
    """HTML-escape one field and keep its line breaks."""
    return html.escape("" if value is None else str(value), quote=False).replace("\t", "    ").replace("\r\n", "\n").replace("\n", "<br>")


def flat(value) -> str:
    """One line, the owner's ' / ' convention for multi-line code."""
    return " / ".join(part.strip() for part in str(value or "").splitlines() if part.strip())


def project_index(edition: dict) -> dict:
    return {p["slug"]: p for p in edition["projects"]}


def deck_name(project: dict) -> str:
    title = (project.get("title") or project["slug"]).replace("::", " ").strip()
    return f"Recurser::{project['code']} {title}"


def tags_for(card: dict) -> list[str]:
    out = [f"rc::type::{card['type']}", f"rc::level::{card['level']}", f"rc::cat::{card['category']}", f"rc::{card['slug'].replace(' ', '_')}"]
    out += [str(t).replace(" ", "_") for t in card.get("tags") or []]
    return out


def permalink(card: dict, project: dict, owner: str) -> str:
    refs = card.get("source_refs") or []
    if not (refs and owner):
        return ""
    r = refs[0]
    at = r.get("commit") or r.get("branch")
    if not at:
        return ""
    url = f"https://github.com/{owner}/{project['repo']}/blob/{at}/{r['path']}"
    return url + (f"#L{r['line']}" if r.get("line") else "")


def source_label(card: dict) -> str:
    refs = card.get("source_refs") or []
    if refs:
        return refs[0]["path"] + (f":{refs[0]['line']}" if refs[0].get("line") else "")
    return (card.get("source") or [""])[0]


def card_note(card: dict, project: dict, owner: str) -> dict | None:
    """Field values for one ``Recurser Card v1`` note, or None when it would generate no card."""
    is_trace = card["type"] == "trace"
    cloze = card.get("cloze") or ""
    keystone = "y" if card["rank"] == 0 else ""
    question = "" if is_trace else card.get("question", "")
    if not (question or card.get("anchor") or card.get("confusables") or keystone):
        return None
    category = schema.CATEGORIES[card["category"]][0]
    return {
        "ID": card["id"],
        "Code": card["code"],
        "Project": esc(project.get("title") or project["slug"]),
        "Category": esc(category),
        "Type": card["type"],
        "Level": str(card["level"]),
        "Concept": esc(card.get("concept", "")),
        "Question": esc(question),
        "Snippet": esc(card.get("code_snippet", "")).replace("<br>", "\n") if card.get("code_snippet") else "",
        "ClozeFront": schema.CLOZE_RE.sub("[...]", esc(cloze)) if cloze and not is_trace else "",
        "Answer": esc(card.get("answer", "")) if not is_trace else esc(" → ".join(card.get("steps") or [])),
        "ClozeBack": schema.CLOZE_RE.sub(lambda m: '<span class="fill">' + m.group(0).split("::", 1)[1][:-2] + "</span>", esc(cloze)) if cloze and not is_trace else "",
        "ExpectedOutput": esc(card.get("expected_output", "")).replace("<br>", "\n") if card.get("expected_output") else "",
        "Explanation": esc(card.get("explanation", "")),
        "Anchor": esc(card.get("anchor", "")),
        "Example": esc(card.get("example", "")).replace("<br>", "\n") if card.get("example") else "",
        "Confusables": esc(card.get("confusables", "")),
        "Source": esc(source_label(card)),
        "Evidence": esc(card.get("evidence_quote", "")).replace("<br>", "\n") if card.get("evidence_quote") else "",
        "Permalink": permalink(card, project, owner),
        "Keystone": keystone,
    }


def step_notes(card: dict, project: dict) -> list[dict]:
    """One ``Recurser Step v1`` note per step: two steps of context, recite one."""
    steps = card.get("steps") or []
    if card["type"] != "trace" or not steps:
        return []
    title = f"{project.get('title') or project['slug']} :: {card.get('concept', '')}"
    out = []
    for k, step in enumerate(steps, start=1):
        context = "<br>".join(esc(s) for s in steps[max(0, k - 3):k - 1]) if k > 1 else "[Beginning]"
        digest = hashlib.sha1(step.encode("utf-8")).hexdigest()[:8]
        out.append({
            "Title": esc(title),
            "Sequence": str(k),
            "Context": context,
            "Line": esc(step) + (" □" if k == len(steps) else ""),
            "Prompt": "[...]",
            "Source": esc(source_label(card)),
            "ID": f"{card['id']}#{digest}",
        })
    return out


def select_cards(edition: dict, progress: dict | None = None, only_learned: bool = False) -> list[dict]:
    """Stable order; leaves out stale cards, cards the owner left out, and (optionally) unlearned ones."""
    flags = (progress or {}).get("flags") or {}
    states = (progress or {}).get("cards") or {}
    out = []
    for c in sorted(edition["cards"], key=lambda c: c["code"]):
        if c.get("stale") or flags.get(c["id"]) == "suspended":
            continue
        if only_learned and not (states.get(c["id"]) or {}).get("learned"):
            continue
        out.append(c)
    return out


# -- writers ------------------------------------------------------------------
def write_classic_csv(edition: dict, out_dir: str, cards: list[dict]) -> list[str]:
    target = os.path.join(out_dir, "export_for_anki")
    os.makedirs(target, exist_ok=True)
    projects, paths = project_index(edition), []
    for slug, project in sorted(projects.items(), key=lambda kv: kv[1]["code"]):
        rows = [c for c in cards if c["slug"] == slug]
        if not rows:
            continue
        path = os.path.join(target, f"{project['code']}-{slug}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh, delimiter=";", lineterminator="\n")
            for c in rows:
                example = c.get("expected_output") if c["type"] == "output" else c.get("example", "")
                answer = " → ".join(c["steps"]) if c["type"] == "trace" and c.get("steps") else c.get("answer", "")
                writer.writerow([c["code"], flat(c.get("concept")), flat(c.get("anchor")), flat(answer), flat(example), flat(c.get("confusables"))])
        paths.append(path)
    return paths


def write_text_files(edition: dict, out_dir: str, cards: list[dict]) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    projects, owner = project_index(edition), edition.get("owner", "")

    def line(values):
        return "\t".join(str(v).replace("\n", "<br>") for v in values)

    card_lines = ["#separator:tab", "#html:true", f"#notetype:{CARD_MODEL}", "#guid column:1", "#deck column:2",
                  f"#tags column:{len(CARD_FIELDS) + 3}", "#columns:" + "\t".join(["GUID", "Deck"] + CARD_FIELDS + ["Tags"])]
    step_lines = ["#separator:tab", "#html:true", f"#notetype:{STEP_MODEL}", "#guid column:1", "#deck column:2",
                  f"#tags column:{len(STEP_FIELDS) + 3}", "#columns:" + "\t".join(["GUID", "Deck"] + STEP_FIELDS + ["Tags"])]
    for c in cards:
        project = projects[c["slug"]]
        note = card_note(c, project, owner)
        tags = " ".join(tags_for(c))
        if note:
            card_lines.append(line([c["id"], deck_name(project)] + [note[f] for f in CARD_FIELDS] + [tags]))
        for step in step_notes(c, project):
            step_lines.append(line([step["ID"], deck_name(project)] + [step[f] for f in STEP_FIELDS] + [tags]))
    paths = []
    for name, lines in (("recurser-cards.txt", card_lines), ("recurser-steps.txt", step_lines)):
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        paths.append(path)
    return paths


def write_apkg(edition: dict, out_dir: str, cards: list[dict]) -> str | None:
    """Package with note types, templates and styling.  Returns None when genanki is not installed."""
    try:
        import genanki
    except ImportError:
        return None
    projects, owner = project_index(edition), edition.get("owner", "")
    card_model = genanki.Model(CARD_MODEL_ID, CARD_MODEL, fields=[{"name": f} for f in CARD_FIELDS], templates=CARD_TEMPLATES, css=CSS)
    step_model = genanki.Model(STEP_MODEL_ID, STEP_MODEL, fields=[{"name": f} for f in STEP_FIELDS], templates=STEP_TEMPLATES, css=CSS, sort_field_index=1)
    decks: dict[str, "genanki.Deck"] = {}
    for c in cards:
        project = projects[c["slug"]]
        name = deck_name(project)
        if name not in decks:
            decks[name] = genanki.Deck(1794000000 + zlib.crc32(name.encode("utf-8")) % 400000000, name)
        note = card_note(c, project, owner)
        tags = [t.replace(" ", "_") for t in tags_for(c)]
        if note:
            decks[name].add_note(genanki.Note(model=card_model, fields=[note[f] for f in CARD_FIELDS], guid=c["id"], tags=tags))
        for step in step_notes(c, project):
            decks[name].add_note(genanki.Note(model=step_model, fields=[step[f] for f in STEP_FIELDS], guid=step["ID"], tags=tags))
    if not decks:
        return None
    path = os.path.join(out_dir, "recurser.apkg")
    try:
        stamp = time.mktime(time.strptime(edition.get("as_of", "")[:10], "%Y-%m-%d"))
    except (ValueError, OverflowError):
        stamp = None
    genanki.Package(list(decks.values())).write_to_file(path, timestamp=stamp)
    return path


def export_all(edition: dict, out_dir: str, *, apkg: bool = True, progress: dict | None = None, only_learned: bool = False) -> list[str]:
    os.makedirs(out_dir, exist_ok=True)
    cards = select_cards(edition, progress, only_learned)
    paths = write_classic_csv(edition, out_dir, cards) + write_text_files(edition, out_dir, cards)
    if apkg:
        package = write_apkg(edition, out_dir, cards)
        if package:
            paths.append(package)
    return paths
