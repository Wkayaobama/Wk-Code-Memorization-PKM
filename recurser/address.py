"""The address space — every card has a fixed four-digit home, ``PP C I``.

Same rule as the owner's Millenium matrices: learn the addresses once, never
reorder, never renumber.

* ``PP`` project code.  Tens digit = portfolio shelf (below), units digit = the
  project's slot on that shelf.  Assigned once and stored in the tier manifest.
* ``C``  category digit (``schema.CATEGORIES``): what kind of knowledge.
* ``I``  rank inside the category, 0 = most fundamental.

So ``2150`` reads: shelf 2, project 1, category 5 (runtime & orchestration),
the most fundamental card on that shelf.
"""
from __future__ import annotations

import re

# Portfolio shelves — the tens digit of a project code.  Names are generic on
# purpose (no client names) so the public edition can ship them unchanged.
DOMAINS = {
    0: ("Workbench", "ground zero: editor, shell, kits and personal skills"),
    1: ("Ingest & load pipelines", "the first mile: sources into bronze and silver"),
    2: ("Warehouse & cloud runs", "two engines: BigQuery and Cloud Run jobs"),
    3: ("CRM logic & HubSpot", "the triangle: contacts, companies, deals"),
    4: ("Ontology & data modelling", "four corners of a schema"),
    5: ("LLM apps & agent harnesses", "five fingers on the model: prompts, tools, runs"),
    6: ("Memory & learning systems", "six sides of a die: drill until it lands"),
    7: ("Media & storytelling", "seven frames: video and decks"),
    8: ("Skill libraries & knowledge transfer", "the infinity loop: write it down, hand it on"),
    9: ("Generated apps & experiments", "the last shelf: scaffolds and trials"),
}

CODE_RE = re.compile(r"^\d{2}$")


class RegistryError(ValueError):
    pass


def check_registry(entries: list[dict]) -> None:
    """Every project needs a unique two-digit code."""
    seen: dict[str, str] = {}
    for e in entries:
        code = e.get("code")
        if not (isinstance(code, str) and CODE_RE.match(code)):
            raise RegistryError(f"project {e.get('slug')!r}: code must be two digits, got {code!r}")
        if code in seen:
            raise RegistryError(f"project code {code} is used by both {seen[code]!r} and {e.get('slug')!r}")
        seen[code] = e.get("slug", "?")


def next_code(domain: int, taken: set[str]) -> str:
    """Next free slot on a shelf; raises when the shelf is full."""
    if domain not in DOMAINS:
        raise RegistryError(f"unknown shelf {domain!r}")
    for unit in range(10):
        code = f"{domain}{unit}"
        if code not in taken:
            return code
    raise RegistryError(f"shelf {domain} ({DOMAINS[domain][0]}) is full — pick another shelf")


def card_code(project_code: str, category: int, rank: int) -> str:
    return f"{project_code}{category}{rank}"


def card_id(slug: str, category: int, rank: int) -> str:
    """Stable identity used by review history and Anki: never changes once issued."""
    return f"{slug}:{category}{rank}"


def split_code(code: str) -> dict:
    """'2150' -> {'project': '21', 'shelf': 2, 'slot': 1, 'category': 5, 'rank': 0}."""
    if not re.match(r"^\d{4}$", code or ""):
        raise RegistryError(f"card code must be four digits, got {code!r}")
    return {"project": code[:2], "shelf": int(code[0]), "slot": int(code[1]), "category": int(code[2]), "rank": int(code[3])}
