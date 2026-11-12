"""The in-memory database — one SQLite connection built from an edition.

Nothing is stored on disk unless you ask (``dump``).  The tables make the
whole knowledge base queryable in one place::

    repos      every repository with its last-push time, tier and forgetting estimate
    projects   the rehearsed projects (code, title, branch, status, ...)
    cards      every flashcard with its four-digit address
    sources    card -> cited file / line / branch
    facts      the knowledge maps, flattened: (slug, kind, position, title, body, path)

Example::

    con = db.build(edition)
    db.query(con, "select code, title, days_since from v_projects order by days_since desc")
"""
from __future__ import annotations

import json
import sqlite3

SCHEMA = """
create table repos (name text primary key, pushed_at text, visibility text, fork integer,
                    days_since integer, tier text, retention real, slug text, code text);
create table projects (slug text primary key, code text unique, repo text, title text, one_liner text,
                       purpose text, domain text, status text, visibility text, fork integer, pushed_at text,
                       branch text, confidence text, aliases text, stack text, card_count integer);
create table cards (id text primary key, slug text, code text unique, category integer, rank integer,
                    type text, level integer, concept text, question text, answer text, explanation text,
                    anchor text, example text, confusables text, cloze text, code_snippet text,
                    expected_output text, steps text, evidence_quote text, tags text);
create table sources (card_id text, path text, line integer, end_line integer, branch text);
create table facts (slug text, kind text, position integer, title text, body text, path text);
create index idx_cards_slug on cards(slug);
create index idx_facts_slug on facts(slug, kind);
create view v_projects as
  select p.code, p.slug, p.title, p.status, p.visibility, p.branch, r.pushed_at, r.days_since, r.tier,
         r.retention, p.card_count
  from projects p left join repos r on lower(r.name) = lower(p.repo);
create view v_cards as
  select c.code, c.slug, p.title as project, c.category, c.rank, c.type, c.level, c.concept, c.question, c.answer
  from cards c join projects p on p.slug = c.slug;
"""

# (map key, title field, body fields, path field)
FACT_KINDS = [
    ("entry_points", "command", ("what",), "path"),
    ("architecture", "component", ("responsibility",), "path"),
    ("key_functions", "name", ("signature", "does", "input_example", "expected_output"), "path"),
    ("infra", "topic", ("detail",), "path"),
    ("decisions", "decision", ("why", "alternative_rejected"), "evidence"),
    ("gotchas", "issue", ("consequence", "fix_or_guard"), "path"),
    ("glossary", "term", ("meaning",), None),
    ("relations", "project", ("relation",), None),
]


def build(edition: dict) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for r in edition["inventory"]:
        con.execute(
            "insert or replace into repos values (?,?,?,?,?,?,?,?,?)",
            (r["name"], r.get("pushed_at"), r.get("visibility"), int(bool(r.get("fork"))), r.get("days_since"),
             r.get("tier"), r.get("retention"), r.get("slug"), r.get("code")),
        )
    for p in edition["projects"]:
        con.execute(
            "insert into projects values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["slug"], p["code"], p["repo"], p.get("title"), p.get("one_liner"), p.get("purpose"), p.get("domain"),
             p.get("status"), p.get("visibility"), int(bool(p.get("fork"))), p.get("pushed_at"), p.get("branch"),
             p.get("confidence"), json.dumps(p.get("aliases") or []), json.dumps(p.get("stack") or []), p.get("card_count", 0)),
        )
        for kind, title_key, body_keys, path_key in FACT_KINDS:
            for i, item in enumerate(p.get(kind) or []):
                body = " — ".join(str(item[k]) for k in body_keys if item.get(k))
                con.execute("insert into facts values (?,?,?,?,?,?)",
                            (p["slug"], kind, i, item.get(title_key), body, item.get(path_key) if path_key else None))
        for kind in ("data_flow", "directions"):
            for i, text in enumerate(p.get(kind) or []):
                con.execute("insert into facts values (?,?,?,?,?,?)", (p["slug"], kind, i, None, text, None))
    for c in edition["cards"]:
        con.execute(
            "insert into cards values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["id"], c["slug"], c["code"], c["category"], c["rank"], c["type"], c["level"], c.get("concept"),
             c.get("question"), c.get("answer"), c.get("explanation"), c.get("anchor"), c.get("example"),
             c.get("confusables"), c.get("cloze"), c.get("code_snippet"), c.get("expected_output"),
             json.dumps(c.get("steps") or []), c.get("evidence_quote"), json.dumps(c.get("tags") or [])),
        )
        for ref in c.get("source_refs") or []:
            con.execute("insert into sources values (?,?,?,?,?)",
                        (c["id"], ref.get("path"), ref.get("line"), ref.get("end"), ref.get("branch")))
    con.commit()
    return con


def query(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def dump(con: sqlite3.Connection, path: str) -> None:
    """Persist the in-memory database to a file (for DB browsers / BI tools)."""
    target = sqlite3.connect(path)
    with target:
        con.backup(target)
    target.close()
