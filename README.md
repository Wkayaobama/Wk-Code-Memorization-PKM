# Recurser

Rehearse your own projects so you never have to relearn them.

Recurser reads your repositories, ranks them by how long ago you last touched each one,
and turns every recent project into a deck of flashcards about what you built:
purpose, flow, key functions and their expected output, infrastructure, and the reasons
behind the decisions. You drill the cards in a small web app that runs from a single
file, on localhost, or from any phone.

Typical questions:

- *What does `to_cm(3.0, unit='ft')` return?*
- *What makes the load pipeline safe to re-run?*
- *Why does the loader merge on a key instead of appending?*
- *Which function lets it load several stations at once?*

## Start in one minute

```bash
git clone -b claude/recurser-code-memorization https://github.com/Wkayaobama/Wk-Code-Memorization-PKM
cd Wk-Code-Memorization-PKM
python3 -m recurser serve          # http://127.0.0.1:8765
```

No dependencies: Python 3.10+ and a browser. Other ways to run it:

| You want | Do this |
|---|---|
| Nothing to install at all | open `docs/index.html` (the whole app in one file) |
| Your phone on the same Wi-Fi | `python3 -m recurser serve --host 0.0.0.0`, then open the printed link with its `?token=` on the phone |
| A file you can mail to yourself | `python3 -m recurser build --single-file dist/recurser.html` |
| GitHub Pages | repository Settings → Pages → folder `/docs` |

Review history is kept in the browser. Served from `recurser serve` it is also saved to
`~/.recurser/progress.json`, so every browser that opens that address shares one history.
Sync → *Save history* and *Merge a saved history file* move it anywhere else.

## How it works

```
your repositories ──► knowledge map + cards (data/<tier>/…json)  ◄── the only thing you edit
                                   │
        ┌──────────────┬───────────┼───────────────┬────────────────┐
        ▼              ▼           ▼               ▼                ▼
   web app bundle   workbook    Anki exports   vault pages    in-memory database
   (app/, docs/)    (.xlsx)     (csv/txt/apkg) (Markdown)     (SQLite, `recurser sql`)
```

**The cards are the source of truth; everything else is regenerated.** Anki comes last.

### Addresses

Every card has a fixed four-digit address that is issued once and never renumbered.

| Digit | Meaning |
|---|---|
| 1 | portfolio shelf (0 workbench … 9 experiments) |
| 2 | the project's slot on that shelf |
| 3 | knowledge shelf: 0 purpose · 1 data model · 2 flow · 3 functions · 4 components · 5 runtime · 6 decisions · 7 storage · 8 infra · 9 quality & ops |
| 4 | rank on that shelf, 0 = most fundamental |

`2150` reads: shelf 2, project 1, runtime and orchestration, the keystone card.
The rank-0 row of a project is its re-entry kit: learn only that row and the project is
back in your hands.

### Question forms

`recall` a fact · `locate` which function or file · `output` predict the result of a call
(or the intended error) · `why` X rather than Y · `trace` recite an ordered chain ·
`cloze` fill the load-bearing token · `compare` two look-alikes, one difference ·
`scenario` something breaks, what do you do. Four levels, from one-line recall for the
commute to trade-off analysis at a desk.

A card can point to its look-alikes in other projects (`see_also`): the linked card opens
in place under the answer, so two pipelines that sound alike are compared where the
confusion happens.

### Sessions

| Session | What it serves |
|---|---|
| Review | whatever is due, most overdue first |
| Learn a new block | up to 10 new cards of ONE project, rank by rank, the most forgotten project first |
| On the go | one-line cards only |
| Predict the output | commit a prediction, then compare; an exact match is recognised |
| Re-entry | a project's rank-0 row with the anchor scene shown as a cue |
| Sit | one project as an exam: attempt before every reveal, pass or fail, one gap note per miss |
| Walk through · Random draw | a project in address order, or ten cards at random (practice only) |
| Evening recap | today's misses and today's new cards |

Scheduling is SM-2: *Again* returns in ten minutes, the first pass waits a day, the second
three, then each gap is multiplied by the card's ease. A card is **Learned** only after an
unaided correct recall at least 24 hours after its previous review; a hint caps the
rating at Hard, and a lapse clears the flag.

### What "in memory" means

A planning estimate, not a measurement. An unseen card fades from the day you last pushed
to its project, half gone every 21 days. A reviewed card fades from its last review, more
slowly the longer its interval. A project's figure is the mean over its cards, and the
project with the lowest figure gets the next block of new cards.

## The decks

| Tier | Built from | Where it lives |
|---|---|---|
| `data/public/` | public repositories only | committed here |
| `data/private/` | private repositories | **git-ignored here**; keep it in a private repository |

`recurser build` merges the tiers it finds. `docs/index.html` is always built from the
public tier alone, and the build refuses to write a private edition into `docs/`.

What ingest does before anything reaches `data/public/`:

- **Private names.** A mention of a private repository (or of a product name the
  manifest lists under `private_terms`) becomes `[private project]`, and a relation to a
  private project is dropped. One exception: a name the public repository already
  spells out itself, in a path or a file, stays, because that repository published it.
- **Identifiers.** Strings listed in the scrub file (`--scrub scrub.json`, shaped
  `{"all": [...], "public": [...]}`) become `[id]`: account, portal or tenant numbers in
  every tier, anything else you choose in the public tier only.
- **Full wording.** When a public deck was redacted, its original wording is kept in
  `data/private/unredacted/`, and the private edition reads that copy.
- **Citations are never rewritten.** `evidence_quote` and the cited paths must keep
  matching the code, so a card whose quote holds something unpublishable has to be
  re-grounded or dropped by its author.

## Commands

```text
python3 -m recurser inventory [--recent --days 180]     last-push table: days, tier, unrehearsed estimate
python3 -m recurser match "tide load"                   fuzzy-find a project by any name you use for it
python3 -m recurser build [--single-file F] [--fragment F]
python3 -m recurser serve [--host H --port P --progress FILE]
python3 -m recurser xlsx out.xlsx [--progress history.json] [--keep-from old.xlsx]
python3 -m recurser anki out_dir [--progress history.json --only-learned] [--no-apkg]
python3 -m recurser kb out_dir [--progress history.json]
python3 -m recurser sql "select code, title, days_since from v_projects order by days_since desc"
python3 -m recurser verify --sources-root DIR [--write]   re-check every card against fresh checkouts
python3 -m recurser validate map|cards FILE [--repo DIR]
python3 -m recurser ingest --manifest … --maps … --cards … --codes … [--scrub scrub.json]
```

`xlsx` needs `pip install openpyxl`; the `.apkg` needs `pip install genanki`. Everything
else is the standard library.

### The workbook

`recurser xlsx` writes the same kind of matrix as a memory-palace sheet: column A is
`Learned (0/1)` with a `=SUM` under it, the code never moves, and the bench and backlog
columns keep the ideas you did not use. Yellow cells are yours; everything else is
regenerated. Tabs: `instructions`, `overview` (live roll-ups: days since, tier, retention,
priority), `matrix`, `sit` (the answer appears only after you type an attempt), `drill`
(press F9 for a random card), `traces`, `taxonomy`, `index`. Pass `--keep-from old.xlsx`
and every yellow cell is carried over by card id.

### Anki

Three routes, by increasing comfort: one semicolon CSV per project
(`Code;Concept;Anchor;Answer;Example;Confusables`), two tab-separated files with Anki
import headers (re-importing updates notes in place), and `recurser.apkg`, which carries
both note types, their conditional card templates and the styling. One address becomes up
to four cards (main, confusable, anchor, address); a blank field produces no card. Each
step of an ordered chain becomes its own next-step card.

## Adding or refreshing a project

Card writing is done by an agent that reads the code; the checks are deterministic.

1. Write a knowledge map following `docs/briefs/MAP_BRIEF.md`.
2. Write the cards following `docs/briefs/CARD_BRIEF.md` and `docs/CARD_PATTERN_SPEC.md`.
3. `python3 -m recurser validate cards FILE --repo CHECKOUT` must end with `OK`. It checks
   the shape, that every cited `path:line` exists, that every card's `evidence_quote`
   appears verbatim in a cited file, and that no credential or e-mail address slipped in.
4. `python3 -m recurser ingest …` files the project into the right tier.
5. `python3 -m recurser build`.

`.claude/skills/recurser/SKILL.md` and `.claude/workflows/` hold the same procedure for
Claude Code: one analyst maps each repository, a writer drafts the deck, a second agent
tries to refute every card against the code, and a third edits for recall quality without
touching facts.

When a project moves on, `recurser verify --sources-root DIR --write` re-checks every
quote against a fresh checkout. A card whose line is gone is marked stale and is no longer
introduced until its deck is refreshed. A fact that is still true keeps its address and
its wording; a retired address is never reused.

## Tests

```bash
python3 -m unittest discover -s tests          # pipeline, standard library only
node tests/test_js_ports.js                    # the JavaScript scheduler and matcher agree with the Python reference
python3 tests/make_vectors.py                  # regenerate the shared vectors after changing either of them
node tests/browser_check.js http://127.0.0.1:8765/ tests/out    # optional end-to-end run, needs playwright
node tests/artifact_sim.js fragment.html       # optional: the hosted-page build against a simulated host
```

Every name in the tests and in the demo edition (`tests/make_demo.py`) is fictional.

## Layout

```
recurser/     schema.py (contract + validators) · fuzzy.py · inventory.py · address.py · store.py
              bundle.py · db.py · srs.py · server.py · xlsx.py · anki.py · kb.py · cli.py
app/          index.html · styles.css · js/ (srs, fuzzy, model, store, session, views, main)
data/         public/ (committed) · private/ (git-ignored)
docs/         index.html (public edition, one file) · CARD_PATTERN_SPEC.md · briefs/
tests/        unit tests, shared scheduler vectors, demo edition, browser checks
.claude/      skills/recurser (the refresh procedure) · workflows/ (map, cards, privacy audit)
```
