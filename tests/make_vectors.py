#!/usr/bin/env python3
"""Regenerate tests/srs_vectors.json from the Python reference.

The JavaScript ports of the scheduler and of the fuzzy matcher are tested against this file
(``node tests/test_js_ports.js``), so a review made on the phone and a review replayed in
Python always land on the same next-due date.  The portfolio below is fictional.

    python3 tests/make_vectors.py
"""
import json
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from recurser import fuzzy, srs  # noqa: E402

PORTFOLIO = [
    {"name": "Bhead-Sonar", "title": "Sonar scout: scrape, extract, score", "keywords": ["fastapi", "extraction"]},
    {"name": "KP-ETL-BQ", "aliases": ["etl big query"], "one_liner": "CSV to warehouse loader with a dbt layer"},
    {"name": "HarborBQ", "title": "Harbor pipeline on cloud run", "keywords": ["bigquery", "cloud run"]},
    {"name": "kp-load", "one_liner": "legacy CRM to marketing hub load pipeline"},
    {"name": "AnchorLPCG", "title": "Lyrics and poetry cloze generator"},
    {"name": "Reef_systems_-_Card_decks", "aliases": ["memory decks", "the matrix sheet"]},
    {"name": "lantern-builder", "title": "Slide deck builder", "keywords": ["presentation", "html"]},
    {"name": "Pier-Ruler", "aliases": ["jetty classifiers"], "one_liner": "CRM governance kit"},
    {"name": "tide-loader", "title": "Tide gauge readings into SQLite"},
    {"name": "harbor-notes", "title": "Notes vault with spaced review"},
    {"name": "app-20270101-000000", "aliases": ["tide rehearsal project"]},
    {"name": "gull-harness", "title": "Agent harness for a second model"},
    {"name": "kelp-maker", "title": "Programmatic video compositions"},
    {"name": "Driftwood-CRM", "title": "Ghost contact manager"},
    {"name": "buoy_sheets_api", "title": "Spreadsheet API helpers"},
    {"name": "Wharf-Ontology", "title": "Entity model and vocabulary"},
    {"name": "seagrass-skills", "title": "Personal skills library"},
    {"name": "TidalRemember", "title": "Wiki notes into flashcards"},
    {"name": "azure-quay-agents", "title": "Agents service workshop"},
    {"name": "xlquay", "title": "Spreadsheet automation from Python"},
    {"name": "kubernetes-the-salty-way", "title": "Cluster bootstrap by hand"},
    {"name": "shellrc", "title": "Terminal and editor configuration"},
    {"name": "Standard-Mooring", "title": "Retrieval baseline"},
    {"name": "cove", "title": "Scratch"},
]
QUERIES = ["headles-bun-sonar", "etl-big-q", "harbor bq", "kp load", "anchor", "kelp", "driftwood crm", "gullharness", "presentation",
           "ontology", "skills", "tidal", "azure agents", "xlquay", "k8s salty way", "memory decks", "tide rehearsal", "jetty", "zzzz qqqq"]
TOKEN_TEXTS = ["HarborBQ", "KP-ETL-BQ", "AnchorLPCG", "Bhead-Sonar", "Reef_systems_-_Card_decks", "the etl big query thing", "app-20270101-000000"]


def srs_vectors(seed: int = 20261112, chains: int = 64) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for _ in range(chains):
        state, now = None, 1_800_000_000_000 + rng.randrange(0, 10**9)
        for _ in range(rng.randrange(3, 11)):
            rating = rng.choices([0, 1, 2, 3], weights=[2, 2, 5, 2])[0]
            new = srs.schedule(state, rating, now)
            out.append({"state": state, "rating": rating, "now": now, "expect": new, "stage": srs.stage(new), "strength": srs.strength(new)})
            state = new
            now = new["due"] + rng.randrange(-3_600_000, 5 * srs.DAY_MS)   # early, on time or days late
    return out


def fuzzy_vectors() -> dict:
    queries = [{"query": q, "top": [{"name": h["item"]["name"], "score": h["score"]} for h in fuzzy.rank(q, PORTFOLIO, limit=3)]} for q in QUERIES]
    return {"items": PORTFOLIO, "queries": queries, "tokens": [{"text": t, "tokens": fuzzy.tokens(t)} for t in TOKEN_TEXTS]}


if __name__ == "__main__":
    data = {"srs": srs_vectors(), "fuzzy": fuzzy_vectors()}
    path = os.path.join(ROOT, "tests", "srs_vectors.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
        fh.write("\n")
    print(f"{path}: {len(data['srs'])} scheduling vectors, {len(data['fuzzy']['queries'])} fuzzy queries")
    for q in data["fuzzy"]["queries"]:
        print(f"  {q['query']:<22} -> " + ", ".join(f"{t['name']} {t['score']}" for t in q["top"]))
