"""Retrieval over an edition: the passages a question is answered from.  Stdlib only.

Every card and every fact of every project map becomes one passage.  A question is scored
against them with BM25 (titles count double, a project named in the question is favoured),
and the best passages are what the assistant is allowed to answer from.  Retrieval runs in
this process, on the edition a person is allowed to see, so a question can never reach a
project outside that person's access.
"""
from __future__ import annotations

import math
import re
from collections import Counter

K1, B = 1.5, 0.75
TITLE_WEIGHT = 2          # a term in the title counts this many times
PROJECT_BOOST = 1.6       # passages of a project the question names
_WORD = re.compile(r"[A-Za-z0-9]+")
_NUMBERING = re.compile(r"^\s*\(?\d{1,2}[.)]\s+")       # one leading "1. " or "(2) ", nothing more
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOP = frozenset(
    "a an and are as at be by do does did for from how i if in is it its my of on or that the their then there this to was "
    "what when where which who why will with would you your can could should has have had not no into about than".split()
)

FACT_KINDS = [   # (map key, label, title key, body keys, path key)
    ("architecture", "component", "component", ("responsibility",), "path"),
    ("key_functions", "function", "name", ("signature", "does", "input_example", "expected_output"), "path"),
    ("entry_points", "command", "command", ("what",), "path"),
    ("infra", "infrastructure", "topic", ("detail",), "path"),
    ("decisions", "decision", "decision", ("why", "alternative_rejected"), "evidence"),
    ("gotchas", "trap", "issue", ("consequence", "fix_or_guard"), "path"),
    ("glossary", "term", "term", ("meaning",), None),
]


def tokens(text: str, *, query: bool = False) -> list[str]:
    """Lower-case words; an identifier also yields its parts (loadStation -> loadstation, load, station)."""
    out: list[str] = []
    for word in _WORD.findall(text or ""):
        low = word.lower()
        if query and low in _STOP:
            continue
        out.append(low)
        parts = _CAMEL.findall(word)
        if len(parts) > 1:
            out.extend(p.lower() for p in parts if len(p) > 1)
    return out


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _short(project: dict) -> str:
    return project.get("repo") or project["slug"]


def passages(edition: dict) -> list[dict]:
    """One passage per card and per fact.  ``blocks`` are the citable pieces of text, in reading order."""
    by_slug = {p["slug"]: p for p in edition["projects"]}
    out: list[dict] = []
    for c in edition["cards"]:
        p = by_slug[c["slug"]]
        code = c["code"]
        answer = " -> ".join(c["steps"]) if c.get("type") == "trace" and c.get("steps") else c.get("answer", "")
        blocks = ["Question: " + _clean(c.get("question")), "Answer: " + _clean(answer)]
        if c.get("code_snippet"):
            blocks.append("Code: " + str(c["code_snippet"]).strip())
        if c.get("expected_output"):
            blocks.append("Output: " + str(c["expected_output"]).strip())
        for key, label in (("explanation", "Why: "), ("confusables", "Do not confuse with: "), ("example", "Example: ")):
            if c.get(key):
                blocks.append(label + _clean(c[key]))
        refs = c.get("source_refs") or []
        out.append({
            "id": "card:" + c["id"], "kind": "card", "card_id": c["id"], "slug": c["slug"], "code": code,
            "title": f"{code[:2]}·{code[2:]} {_short(p)}: {_clean(c.get('concept'))}",
            "source": "recurser://card/" + c["id"],
            "blocks": blocks,
            "path": ", ".join(r["path"] + (f":{r['line']}" if r.get("line") else "") for r in refs[:3]),
        })
    for p in edition["projects"]:
        slug, name = p["slug"], _short(p)

        def fact(kind, n, title, blocks, path=""):
            blocks = [b for b in blocks if b and b.strip()]
            if blocks:
                out.append({"id": f"map:{slug}:{kind}:{n}", "kind": "fact", "slug": slug, "code": p["code"],
                            "title": f"{p['code']} {name}: {title}", "source": f"recurser://map/{slug}/{kind}/{n}",
                            "blocks": blocks, "path": path or ""})

        fact("purpose", 0, "purpose", [_clean(p.get("one_liner")), _clean(p.get("purpose"))])
        if p.get("data_flow"):
            steps = [_clean(_NUMBERING.sub("", str(s))) for s in p["data_flow"]]
            fact("data_flow", 0, "data flow", [f"{i}. {step}" for i, step in enumerate(steps, start=1)])
        for key, label, title_key, body_keys, path_key in FACT_KINDS:
            for n, item in enumerate(p.get(key) or []):
                if not isinstance(item, dict):
                    continue
                title = _clean(item.get(title_key))
                fact(key, n, f"{label} {title}"[:160], [title] + [_clean(item.get(k)) for k in body_keys], item.get(path_key) if path_key else "")
        if p.get("directions"):
            fact("directions", 0, "next directions", [_clean(d) for d in p["directions"]])
    return out


class Index:
    """BM25 over the passages of one edition."""

    def __init__(self, edition: dict):
        self.passages = passages(edition)
        self.docs: list[Counter] = []
        self.lengths: list[int] = []
        df: Counter = Counter()
        for p in self.passages:
            terms = Counter(tokens(" ".join(p["blocks"])))
            for term, n in Counter(tokens(p["title"])).items():
                terms[term] += n * TITLE_WEIGHT
            self.docs.append(terms)
            self.lengths.append(sum(terms.values()))
            df.update(terms.keys())
        self.n = len(self.passages)
        self.avg = (sum(self.lengths) / self.n) if self.n else 0.0
        self.idf = {t: math.log(1 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        # the ways a project is named: its slug, its repository, its nicknames
        self.names: dict[str, list[frozenset]] = {}
        for p in edition["projects"]:
            spellings = [p["slug"], p.get("repo") or ""] + list(p.get("aliases") or [])
            sets = {frozenset(t for t in tokens(s) if t not in _STOP) for s in spellings}
            self.names[p["slug"]] = [s for s in sets if s]

    def named_projects(self, query_terms: set[str]) -> set[str]:
        return {slug for slug, sets in self.names.items() if any(s <= query_terms for s in sets)}

    def search(self, query: str, k: int = 8, slugs=None, per_project: int = 6) -> list[dict]:
        """The k best passages for a question, best first.  ``slugs`` limits the search to some projects."""
        terms = tokens(query, query=True)
        if not terms or not self.n:
            return []
        wanted = set(slugs) if slugs else None
        named = self.named_projects(set(terms))
        counts = Counter(terms)
        scored = []
        for i, doc in enumerate(self.docs):
            passage = self.passages[i]
            if wanted is not None and passage["slug"] not in wanted:
                continue
            score = 0.0
            norm = K1 * (1 - B + B * self.lengths[i] / self.avg) if self.avg else K1
            for term, qn in counts.items():
                tf = doc.get(term)
                if tf:
                    score += self.idf.get(term, 0.0) * tf * (K1 + 1) / (tf + norm) * min(qn, 2)
            if score <= 0:
                continue
            if passage["slug"] in named:
                score *= PROJECT_BOOST
            scored.append((score, i))
        scored.sort(key=lambda x: (-x[0], x[1]))
        out, per = [], Counter()
        for score, i in scored:
            passage = self.passages[i]
            if wanted is None and per[passage["slug"]] >= per_project:
                continue           # one project may not crowd every other out of an open question
            per[passage["slug"]] += 1
            out.append({**passage, "score": round(score, 4)})
            if len(out) >= k:
                break
        return out
