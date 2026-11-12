#!/usr/bin/env python3
"""Write a small fictional edition (two projects) used by the tests and as a first-run demo.

    python3 tests/make_demo.py tests/fixtures/demo
"""
import json
import os
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "fixtures", "demo")
TIER = os.path.join(OUT, "data", "public")


def card(cat, rank, ctype, level, concept, question, answer, **extra):
    base = {"category": cat, "rank": rank, "type": ctype, "level": level, "concept": concept, "question": question,
            "answer": answer, "source": [extra.pop("src", "README.md:1")],
            "evidence_quote": extra.pop("quote", "tide-loader loads tide gauge readings"), "tags": extra.pop("tags", [])}
    base.update(extra)
    return base


TIDE_CARDS = [
    card(0, 0, "recall", 1, "What tide-loader is", "What does tide-loader do, in one sentence?",
         "It loads tide-gauge CSV exports into a warehouse table, one idempotent batch per station and day.",
         anchor="A harbour crane lowering one labelled crate per station onto the quay — never the same crate twice.",
         confusables="harbor-notes — that one only stores text notes; tide-loader moves measurements.",
         see_also=["harbor-notes:00", "harbor-notes:60", "ghost-project:00"]),
    card(0, 1, "why", 2, "Why batches are per station-day", "Why does tide-loader batch by station and day rather than by file?",
         "A file can mix stations; station-day is the smallest unit that can be reloaded without touching its neighbours.",
         explanation="Reloading a whole file would rewrite healthy stations whenever one station is corrected upstream."),
    card(1, 0, "recall", 1, "Reading row contract", "Which four columns must every tide-loader reading carry?",
         "station_id, observed_at (UTC), level_cm, quality_flag."),
    card(1, 1, "cloze", 2, "Idempotency key", "Complete tide-loader's idempotency key.",
         "station_id + observed day", cloze="batch_key = {{c1::station_id}} + '/' + {{c2::observed_at.date()}}"),
    card(2, 0, "trace", 3, "Load pipeline order", "Recite the stages of one tide-loader run, in order.",
         "discover, parse, validate, stage, merge, receipt",
         steps=["discover new CSV exports in the drop folder", "parse rows into Reading objects", "validate units and quality flags",
                "stage the batch in a temporary table", "merge on batch_key so reruns replace instead of duplicating", "write a receipt row with counts"]),
    card(3, 0, "output", 2, "to_cm on feet", "What does to_cm return for a reading given in feet?",
         "30.48 times the value, rounded to one decimal.", code_snippet="to_cm(3.0, unit='ft')", expected_output="91.4", tags=["grounded:logic"],
         explanation="1 ft = 30.48 cm; 3.0 ft = 91.44, rounded to 91.4."),
    card(3, 1, "output", 3, "to_cm on an unknown unit", "What happens when to_cm receives unit='fathom'?",
         "It raises ValueError naming the unit; nothing is converted.", code_snippet="to_cm(2.0, unit='fathom')",
         expected_output="ValueError: unknown unit 'fathom'", tags=["intended-error", "grounded:test"]),
    card(3, 2, "locate", 1, "Where reruns are made safe", "Which function makes a tide-loader rerun safe?",
         "merge_batch() in load/merge.py — it deletes the batch_key rows, then inserts, inside one transaction."),
    card(5, 0, "locate", 2, "Async entry point", "Which function lets tide-loader load several stations concurrently?",
         "run_all() gathers one load_station() coroutine per station under a semaphore of 4.",
         confusables="load_station() — that loads ONE station; run_all() is the only place concurrency lives."),
    card(6, 0, "why", 3, "Merge instead of append", "Why does tide-loader merge on batch_key instead of appending rows?",
         "Upstream re-issues corrected files; an append would double-count every corrected day.",
         explanation="The receipt table would also disagree with the warehouse counts after the first correction.",
         confusables="append-only with a dedup view — rejected: readers would have to remember to use the view."),
    card(6, 1, "compare", 2, "Stage table vs target table", "What separates tide-loader's stage table from its target table?",
         "The stage table is temporary and untyped; the target is typed and only ever changed by merge_batch().",
         confusables="stage = scratch, dropped after the run; target = the single source readers query."),
    card(8, 0, "recall", 1, "Where it runs", "Where does tide-loader run in production?",
         "As a nightly container job; the drop folder is a mounted bucket."),
    card(9, 0, "scenario", 4, "A station doubles overnight", "A station shows twice the usual row count this morning. What do you check, in order?",
         "(1) The receipt row for that batch_key. (2) Whether merge_batch ran inside its transaction. (3) The upstream file for duplicated rows.",
         explanation="A doubled count with a single receipt means the delete half of the merge did not run."),
]

NOTES_CARDS = [
    card(0, 0, "recall", 1, "What harbor-notes is", "What is harbor-notes?", "A command-line notebook that stores one Markdown file per note and a JSON index.",
         quote="harbor-notes keeps one Markdown file per note"),
    card(2, 0, "trace", 2, "Adding a note", "What happens, in order, when you run `harbor add`?", "slug, file, index, print",
         steps=["slugify the title", "write the Markdown file", "append the entry to index.json", "print the path"], quote="harbor-notes keeps one Markdown file per note"),
    card(3, 0, "output", 1, "slugify", "What does slugify return for 'High Tide!'?", "high-tide", code_snippet="slugify('High Tide!')",
         expected_output="high-tide", tags=["grounded:doc"], quote="harbor-notes keeps one Markdown file per note"),
    card(6, 0, "why", 2, "Files, not a database", "Why does harbor-notes store plain files instead of a database?",
         "So notes stay readable and diffable without the tool.", quote="harbor-notes keeps one Markdown file per note",
         see_also=["tide-loader:00"]),
    card(9, 0, "scenario", 3, "Index out of sync", "index.json lists a note whose file is gone. What repairs it?",
         "`harbor reindex` rebuilds the index from the files on disk.", quote="harbor-notes keeps one Markdown file per note"),
    card(1, 0, "cloze", 1, "Index entry shape", "Complete the shape of one index entry.", "slug, title, created",
         cloze="{\"slug\": {{c1::\"high-tide\"}}, \"title\": \"High Tide!\", \"created\": {{c2::\"2026-05-02\"}}}", quote="harbor-notes keeps one Markdown file per note"),
]


def pmap(slug, repo, title, one_liner, status="active"):
    return {
        "slug": slug, "repo": repo, "title": title, "one_liner": one_liner,
        "purpose": one_liner + " This fictional project exists only to show what a knowledge map and its cards look like.",
        "domain": "demo", "status": status, "stack": ["Python 3.11"], "aliases": ["the " + slug.split("-")[0] + " thing"],
        "entry_points": [{"command": f"python -m {slug.replace('-', '_')}", "what": "run it", "path": "README.md"}],
        "architecture": [{"component": "Core", "path": "README.md", "responsibility": "Everything, in this demo.", "talks_to": ["disk"]}],
        "data_flow": ["1. read input", "2. transform", "3. write output"],
        "key_functions": [{"name": "main", "path": "README.md:1", "signature": "def main() -> int", "does": "Runs the tool.", "expected_output": "0"}],
        "infra": [{"topic": "Runtime", "detail": "A nightly container job.", "path": "README.md"}],
        "decisions": [{"decision": "Idempotent batches", "why": "Reruns must be safe.", "alternative_rejected": "append-only", "evidence": "README.md:1"}],
        "gotchas": [{"issue": "Mixed units upstream", "consequence": "Wrong levels", "fix_or_guard": "to_cm validates the unit", "path": "README.md"}],
        "glossary": [{"term": "batch_key", "meaning": "station id plus observed day"}],
        "relations": [], "directions": ["Add a second gauge vendor."], "confidence": "high", "coverage_notes": "Fictional demo data.",
    }


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)


write(os.path.join(TIER, "projects.json"), {"as_of": "2026-11-12", "owner": "demo-owner", "projects": [
    {"slug": "tide-loader", "repo": "tide-loader", "code": "10", "visibility": "public", "fork": False, "pushed_at": "2026-08-03T10:00:00Z",
     "branch": "main", "alts": [], "aliases": ["tide loader", "the gauge pipeline"], "analyzed_at": "2026-11-12"},
    {"slug": "harbor-notes", "repo": "harbor-notes", "code": "60", "visibility": "public", "fork": False, "pushed_at": "2026-11-02T09:00:00Z",
     "branch": "main", "alts": [], "aliases": ["harbour notebook"], "analyzed_at": "2026-11-12"}]})
write(os.path.join(TIER, "inventory.json"), {"as_of": "2026-11-12", "owner": "demo-owner", "repos": [
    {"name": "tide-loader", "pushed_at": "2026-08-03T10:00:00Z", "visibility": "public", "fork": False},
    {"name": "harbor-notes", "pushed_at": "2026-11-02T09:00:00Z", "visibility": "public", "fork": False},
    {"name": "old-experiments", "pushed_at": "2024-02-11T09:00:00Z", "visibility": "public", "fork": False},
    {"name": "someone-elses-lib", "pushed_at": "2026-10-20T09:00:00Z", "visibility": "public", "fork": True}]})
for slug, cards in (("tide-loader", TIDE_CARDS), ("harbor-notes", NOTES_CARDS)):
    for c in cards:
        c["source_refs"] = [{"path": "README.md", "line": 1, "branch": "main"}]
    write(os.path.join(TIER, "cards", f"{slug}.json"), {"slug": slug, "cards": cards})
write(os.path.join(TIER, "maps", "tide-loader.json"), pmap("tide-loader", "tide-loader", "Tide Loader", "Loads tide-gauge CSV exports into a warehouse, one idempotent batch per station and day."))
write(os.path.join(TIER, "maps", "harbor-notes.json"), pmap("harbor-notes", "harbor-notes", "Harbor Notes", "A command-line notebook that keeps one Markdown file per note and a JSON index."))
print("demo edition written to", OUT)
