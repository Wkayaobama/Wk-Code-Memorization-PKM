"""Unit tests for the Recurser pipeline.  Stdlib only:  python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from recurser import address, anki, bundle, db, fuzzy, inventory, kb, schema, server, srs, store  # noqa: E402

DEMO = os.path.join(ROOT, "tests", "fixtures", "demo")
TODAY = date(2026, 11, 12)


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dump(obj, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _text(path, encoding="utf-8"):
    with open(path, encoding=encoding) as fh:
        return fh.read()


def demo_edition():
    if not os.path.exists(os.path.join(DEMO, "data", "public", "projects.json")):
        subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_demo.py"), DEMO], check=True, capture_output=True)
    return store.load_edition(DEMO, ["public"], TODAY)


def good_card(**over):
    card = {"category": 3, "rank": 0, "type": "recall", "level": 1, "concept": "entry point", "question": "Which command starts demo?",
            "answer": "python -m demo", "source": ["src/app.py:2"], "evidence_quote": "def main() -> int:", "tags": []}
    card.update(over)
    return card


class RepoCase(unittest.TestCase):
    """A throwaway checkout to ground cards against."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "demo")
        os.makedirs(os.path.join(self.repo, "src"))
        os.makedirs(os.path.join(self.repo, ".claude", "skills"))
        with open(os.path.join(self.repo, "src", "app.py"), "w") as fh:
            fh.write("import sys\ndef main() -> int:\n    return 0\n")
        with open(os.path.join(self.repo, ".claude", "skills", "SKILL.md"), "w") as fh:
            fh.write("# skill\nAlways run the gate before a write.\n")

    def tearDown(self):
        self.tmp.cleanup()

    def deck(self, *cards):
        base = [good_card(rank=i, question=f"Which command starts demo, variant {i}?", concept=f"entry point {i}") for i in range(6)]
        return {"slug": "demo", "cards": list(cards) + base[len(cards):]}


class SchemaTests(RepoCase):
    def test_valid_deck_passes_with_grounding(self):
        rep = schema.validate_cards(self.deck(), [self.repo])
        self.assertTrue(rep.ok, rep.errors)

    def test_dot_directories_are_real_paths(self):
        card = good_card(source=[".claude/skills/SKILL.md:2"], evidence_quote="Always run the gate before a write.")
        self.assertTrue(schema.validate_cards(self.deck(card), [self.repo]).ok)
        self.assertEqual(schema.split_source("./src/app.py:2"), ("src/app.py", 2, 2))

    def test_missing_path_bad_line_and_reversed_range_fail(self):
        for src in (["src/nope.py:1"], ["src/app.py:99"], ["src/app.py:3-1"], ["../etc/passwd"], ["/abs/path.py"]):
            rep = schema.validate_cards(self.deck(good_card(source=src)), [self.repo])
            self.assertFalse(rep.ok, src)

    def test_quote_must_be_verbatim(self):
        rep = schema.validate_cards(self.deck(good_card(evidence_quote="def main() returns zero always")), [self.repo])
        self.assertTrue(any("evidence_quote" in e for e in rep.errors))
        spaced = good_card(evidence_quote="def   main()  ->  int:")  # whitespace may be collapsed, nothing else
        self.assertTrue(schema.validate_cards(self.deck(spaced), [self.repo]).ok)

    def test_secrets_and_emails_are_refused(self):
        leaked = good_card(answer="token = '" + "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789AB" + "'")   # assembled here so no token-shaped literal sits in the file
        self.assertTrue(any("secret" in e for e in schema.validate_cards(self.deck(leaked), [self.repo]).errors))
        mail = good_card(explanation="ask jane.doe" + "@" + "acme-corp.io")
        self.assertTrue(any("e-mail" in e for e in schema.validate_cards(self.deck(mail), [self.repo]).errors))
        allowed = good_card(explanation="clone with git@github.com:owner/repo.git")
        self.assertTrue(schema.validate_cards(self.deck(allowed), [self.repo]).ok)

    def test_scanner_reads_credentials_in_their_common_shapes(self):
        j = "".join    # every sample is assembled here so that no credential-shaped literal sits in this file
        leaks = [
            'HUBSPOT_API_KEY = "' + j(["a1b2c3d4e5", "f6a7b8c9d0", "e1f2a3b4c5"]) + '"',
            "DB_PASSWORD = '" + j(["Sup3rSecret", "Passw0rd99"]) + "'",
            '"api_key": "' + j(["9f8e7d6c5b4a", "3210fedcba98"]) + '"',
            "API_KEY=" + j(["abcd1234", "efgh5678", "ijkl9012"]),
            "Authorization: Bearer " + j(["abcDEF123456", "ghiJKL789012", "mnoPQR345678"]),
            "postgres://etl:" + j(["S3cr3t", "Pw!"]) + "@db.internal.net:5432/crm",
            j(["sk-", "proj-", "Abc123def456Ghi789jkl012Mno345"]),
            j(["pat-", "eu1-", "12345678-1234-1234-1234-123456789abc"]),
            "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----",
            "mail admin" + "@" + "realcompany.com",
        ]
        fine = [
            "Set ANTHROPIC_API_KEY in the environment; the value is never logged.",
            "token = os.environ['HUBSPOT_TOKEN']",
            "api_key = settings.anthropic_api_key",
            "API_KEY=<your-key-here>",
            "postgres://user:password@localhost:5432/db",
            "Authorization: Bearer $HUBSPOT_TOKEN",
            "max_tokens = 4096",
            "then [PagingToken = Text.From(Record.Field(results, offsetField))]",
            "write to user@example.com or git@github.com:owner/repo.git",
        ]
        for text in leaks:
            self.assertTrue(schema.scan_text(text), text[:40])
        for text in fine:
            self.assertEqual(schema.scan_text(text), [], text)

    def test_type_specific_fields(self):
        self.assertFalse(schema.validate_cards(self.deck(good_card(type="output")), [self.repo]).ok)
        self.assertFalse(schema.validate_cards(self.deck(good_card(type="cloze", cloze="no deletion here")), [self.repo]).ok)
        self.assertFalse(schema.validate_cards(self.deck(good_card(type="trace", steps=["only", "two"])), [self.repo]).ok)
        self.assertFalse(schema.validate_cards(self.deck(good_card(type="compare")), [self.repo]).ok)
        ok = good_card(type="output", code_snippet="main()", expected_output="0", tags=["grounded:logic"])
        self.assertTrue(schema.validate_cards(self.deck(ok), [self.repo]).ok)

    def test_addresses_are_unique_and_retired_ones_stay_retired(self):
        deck = self.deck()
        deck["cards"][1]["rank"] = 0
        self.assertTrue(any("address already used" in e for e in schema.validate_cards(deck, [self.repo]).errors))
        deck = self.deck()
        deck["retired"] = ["30"]
        self.assertTrue(any("retired" in e for e in schema.validate_cards(deck, [self.repo]).errors))

    def test_cross_links_are_checked_for_shape(self):
        good = self.deck(good_card(see_also=["tide-loader:31", "harbor_notes.v2:00"]))
        self.assertTrue(schema.validate_cards(good, [self.repo]).ok)
        for bad in (["tide-loader"], ["tide-loader:3"], "tide-loader:31", ["a:00", "a:00"], ["a:00", "b:00", "c:00", "d:00", "e:00"]):
            self.assertFalse(schema.validate_cards(self.deck(good_card(see_also=bad)), [self.repo]).ok, bad)

    def test_lints_warn_without_failing(self):
        rep = schema.validate_cards(self.deck(good_card(question="Does demo start with python?", extra_field=1)), [self.repo])
        self.assertTrue(rep.ok)
        text = " ".join(rep.warnings)
        self.assertIn("yes/no", text)
        self.assertIn("outside the contract", text)

    def test_map_validation(self):
        demo_edition()
        pmap = _load(os.path.join(DEMO, "data", "public", "maps", "tide-loader.json"))
        self.assertTrue(schema.validate_map(pmap).ok)
        broken = dict(pmap, status="mysterious")
        self.assertFalse(schema.validate_map(broken).ok)


class FuzzyTests(unittest.TestCase):
    ITEMS = [{"name": n} for n in ("Bhead-Sonar", "KP-ETL-BQ", "HarborBQ", "kp-load", "AnchorLPCG", "Reef_systems_-_Card_decks", "lantern-builder", "Pier-Ruler")]

    def top(self, query, items=None):
        hits = fuzzy.rank(query, items or self.ITEMS, limit=1)
        return hits[0]["item"]["name"] if hits else None

    def test_nicknames_find_their_repository(self):
        self.assertEqual(self.top("headles-bun-sonar"), "Bhead-Sonar")
        self.assertEqual(self.top("etl-big-q"), "KP-ETL-BQ")
        self.assertEqual(self.top("harbor bq"), "HarborBQ")
        self.assertEqual(self.top("kp load"), "kp-load")
        self.assertEqual(self.top("anchor"), "AnchorLPCG")

    def test_aliases_beat_names(self):
        items = self.ITEMS + [{"name": "app-20270101-000000", "aliases": ["lantern repetition project"]}]
        self.assertEqual(self.top("lantern repetition", items), "app-20270101-000000")

    def test_tokens_and_acronyms(self):
        self.assertEqual(fuzzy.tokens("HarborBQ"), ["harbor", "bq"])
        self.assertEqual(fuzzy.tokens("KP-ETL-BQ"), ["kp", "etl", "bq"])
        self.assertGreater(fuzzy.field_score("big q", "bq"), 0.6)
        self.assertEqual(fuzzy.rank("zzzz qqqq", self.ITEMS), [])


class InventoryTests(unittest.TestCase):
    def test_tiers_and_retention(self):
        repos = inventory.analyze([
            {"name": "a", "pushed_at": "2026-11-10T00:00:00Z", "visibility": "public", "fork": False},
            {"name": "b", "pushed_at": "2026-07-06T21:37:18Z", "visibility": "private", "fork": False},
            {"name": "c", "pushed_at": "2023-01-01T00:00:00Z", "visibility": "public", "fork": True},
        ], TODAY)
        self.assertEqual([r["name"] for r in repos], ["a", "b", "c"])
        self.assertEqual([r["tier"] for r in repos], ["hot", "cold", "frozen"])
        self.assertEqual(repos[1]["days_since"], 129)
        self.assertEqual([r["name"] for r in inventory.select_recent(repos, days=180)], ["a", "b"])
        self.assertEqual(inventory.retention(21), 0.5)
        self.assertGreater(inventory.retention(90, mastery=1.0), inventory.retention(90, mastery=0.0))

    def test_snapshot_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tsv = os.path.join(tmp, "s.tsv")
            with open(tsv, "w") as fh:
                fh.write("one\t2026-01-01T00:00:00Z\tpublic\t0\ntwo\t2026-02-01T00:00:00Z\tprivate\t1\n")
            repos = inventory.load_snapshot(tsv)
            self.assertEqual(repos[1], {"name": "two", "pushed_at": "2026-02-01T00:00:00Z", "visibility": "private", "fork": True})
            js = os.path.join(tmp, "s.json")
            with open(js, "w") as fh:
                json.dump({"repos": [{"full_name": "me/three", "private": True, "fork": False, "pushed_at": "2026-03-01T00:00:00Z"}]}, fh)
            self.assertEqual(inventory.load_snapshot(js)[0]["visibility"], "private")


class SrsTests(unittest.TestCase):
    NOW = 1_800_000_000_000

    def test_progression(self):
        s = srs.schedule(None, 2, self.NOW)
        self.assertEqual((s["interval"], s["reps"]), (1.0, 1))
        s = srs.schedule(s, 2, s["due"])
        self.assertEqual(s["interval"], 3.0)
        s = srs.schedule(s, 2, s["due"])
        self.assertEqual(s["interval"], 7.5)
        again = srs.schedule(s, 0, s["due"])
        self.assertEqual((again["reps"], again["lapses"], again["due"] - s["due"]), (0, 1, srs.AGAIN_DELAY_MS))
        self.assertEqual(again["ease"], 2.3)

    def test_ease_floor_and_stages(self):
        s = None
        for _ in range(12):
            s = srs.schedule(s, 0, self.NOW)
        self.assertEqual(s["ease"], srs.MIN_EASE)
        self.assertEqual(srs.stage(None), "new")
        self.assertEqual(srs.stage(s), "learning")
        mature = {"last": 1, "reps": 5, "interval": 30}
        self.assertEqual(srs.stage(mature), "mature")
        self.assertEqual(srs.mastery([mature, None]), 0.5)

    def test_vectors_file_matches_reference(self):
        vectors = _load(os.path.join(ROOT, "tests", "srs_vectors.json"))["srs"]
        for v in vectors[:120]:
            self.assertEqual(srs.schedule(v["state"], v["rating"], v["now"]), v["expect"])


class AddressTests(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(address.card_code("21", 5, 0), "2150")
        self.assertEqual(address.card_id("tide-loader", 3, 1), "tide-loader:31")
        self.assertEqual(address.split_code("2150"), {"project": "21", "shelf": 2, "slot": 1, "category": 5, "rank": 0})
        self.assertEqual(address.next_code(2, {"20", "21"}), "22")
        with self.assertRaises(address.RegistryError):
            address.check_registry([{"slug": "a", "code": "10"}, {"slug": "b", "code": "10"}])
        with self.assertRaises(address.RegistryError):
            address.next_code(3, {f"3{i}" for i in range(10)})


class StoreAndBundleTests(RepoCase):
    def test_ingest_files_only_grounded_projects_and_splits_tiers(self):
        work = os.path.join(self.tmp.name, "work")
        os.makedirs(os.path.join(work, "maps"))
        os.makedirs(os.path.join(work, "cards"))
        pmap = _load(os.path.join(DEMO, "data", "public", "maps", "tide-loader.json"))
        for slug in ("demo", "broken"):
            m = dict(pmap, slug=slug, repo=slug)
            for key in ("entry_points", "architecture", "key_functions", "infra", "decisions", "gotchas"):
                m[key] = [dict(it, **{k: "src/app.py" for k in ("path", "evidence") if k in it}) for it in m[key]]
            _dump(m, os.path.join(work, "maps", f"{slug}.json"))
        _dump(self.deck(), os.path.join(work, "cards", "demo.json"))
        bad = self.deck(good_card(evidence_quote="this sentence is not in the file"))
        bad["slug"] = "broken"
        _dump(bad, os.path.join(work, "cards", "broken.json"))
        manifest = {"as_of": "2026-11-12", "owner": "me", "projects": [
            {"slug": "demo", "repo": "demo", "visibility": "private", "fork": False, "pushed_at": "2026-10-01T00:00:00Z", "dir": self.repo, "branch": "main", "alts": []},
            {"slug": "broken", "repo": "broken", "visibility": "public", "fork": False, "pushed_at": "2026-10-01T00:00:00Z", "dir": self.repo, "branch": "main", "alts": []}]}
        _dump(manifest, os.path.join(work, "projects.json"))
        snap = os.path.join(work, "snap.tsv")
        with open(snap, "w") as fh:
            fh.write("demo\t2026-10-01T00:00:00Z\tprivate\t0\nopen-thing\t2026-09-01T00:00:00Z\tpublic\t0\n")
        out = os.path.join(self.tmp.name, "out")
        report = store.ingest(out, manifest_path=os.path.join(work, "projects.json"), maps_dir=os.path.join(work, "maps"),
                              cards_dir=os.path.join(work, "cards"), codes={"demo": "10", "broken": "11"}, snapshot_path=snap, log=lambda *_: None)
        self.assertEqual([f["slug"] for f in report["filed"]], ["demo"])
        self.assertEqual(report["skipped"][0]["slug"], "broken")
        self.assertTrue(os.path.exists(os.path.join(out, "data", "private", "cards", "demo.json")))
        self.assertFalse(os.path.exists(os.path.join(out, "data", "public", "cards")))
        public_inv = _load(os.path.join(out, "data", "public", "inventory.json"))["repos"]
        self.assertEqual([r["name"] for r in public_inv], ["open-thing"])   # a private name never lands in the public tier
        edition = store.load_edition(out, ["private"], TODAY)
        self.assertEqual(edition["cards"][0]["id"], "demo:30")
        self.assertEqual(edition["cards"][0]["code"], "1030")
        self.assertEqual(edition["cards"][0]["source_refs"][0]["path"], "src/app.py")
        self.assertEqual(edition["edition"], "private")
        # staleness: the cited line disappears from the checkout
        sources = os.path.join(self.tmp.name, "fresh")
        os.makedirs(os.path.join(sources, "demo", "src"))
        with open(os.path.join(sources, "demo", "src", "app.py"), "w") as fh:
            fh.write("import sys\n\ndef start():\n    return 0\n")
        report = store.find_stale(out, sources, ["private"])
        self.assertEqual(len(report["stale"]["demo"]), 6)
        store.write_stale(out, report, ["private"])
        self.assertTrue(all(c.get("stale") for c in store.load_edition(out, ["private"], TODAY)["cards"]))

    def _work(self, slug, visibility, deck, relations=()):
        work = os.path.join(self.tmp.name, "work-" + slug)
        os.makedirs(os.path.join(work, "maps"))
        os.makedirs(os.path.join(work, "cards"))
        pmap = dict(_load(os.path.join(DEMO, "data", "public", "maps", "tide-loader.json")), slug=slug, repo=slug)
        for key in ("entry_points", "architecture", "key_functions", "infra", "decisions", "gotchas"):
            pmap[key] = [dict(it, **{k: "src/app.py" for k in ("path", "evidence") if k in it}) for it in pmap[key]]
        pmap["relations"] = list(relations)
        _dump(pmap, os.path.join(work, "maps", f"{slug}.json"))
        _dump(dict(deck, slug=slug), os.path.join(work, "cards", f"{slug}.json"))
        _dump({"as_of": "2026-11-12", "owner": "me", "projects": [
            {"slug": slug, "repo": slug, "visibility": visibility, "fork": False, "pushed_at": "2026-10-01T00:00:00Z",
             "dir": self.repo, "branch": "main", "alts": []},
            {"slug": "secret-vault", "repo": "Secret-Vault", "visibility": "private", "fork": False, "pushed_at": "2026-10-02T00:00:00Z",
             "dir": self.repo, "branch": "main", "alts": [], "private_terms": ["the vault thing"]}]}, os.path.join(work, "projects.json"))
        with open(os.path.join(work, "snap.tsv"), "w") as fh:
            fh.write(f"{slug}\t2026-10-01T00:00:00Z\t{visibility}\t0\nSecret-Vault\t2026-10-02T00:00:00Z\tprivate\t0\nPayroll_Sync\t2026-01-02T00:00:00Z\tprivate\t0\n")
        return work

    def _ingest(self, work, out, slug):
        return store.ingest(out, manifest_path=os.path.join(work, "projects.json"), maps_dir=os.path.join(work, "maps"),
                            cards_dir=os.path.join(work, "cards"), codes={slug: "50", "secret-vault": "51"},
                            snapshot_path=os.path.join(work, "snap.tsv"), aliases={slug: ["open demo", "portal 7700123 demo"]},
                            scrub={"all": ["7700123"], "public": ["acme-bq"]}, log=lambda *_: None)

    def test_public_decks_never_name_private_projects(self):
        leaky = self.deck(
            good_card(answer="python -m demo, the same entry point as Secret-Vault and payroll sync",
                      explanation="Borrowed from the vault thing. Runs against portal 7700123 in project acme-bq."),
            good_card(rank=1, question="Which command starts demo, variant 1?", concept="entry point 1",
                      evidence_quote="def main() -> int:", code_snippet="# secret_vault style\nmain()", type="recall",
                      see_also=["secret-vault:30", "open-demo:30"]))
        work = self._work("open-demo", "public", leaky, relations=[{"project": "Secret-Vault", "relation": "shares the entry point"},
                                                                    {"project": "open-thing", "relation": "sibling"}])
        out = os.path.join(self.tmp.name, "out")
        report = self._ingest(work, out, "open-demo")
        self.assertEqual([(f["slug"], f["tier"]) for f in report["filed"]], [("open-demo", "public")])
        self.assertGreaterEqual(report["filed"][0]["redactions"], 3)
        public_cards = _load(os.path.join(out, "data", "public", "cards", "open-demo.json"))
        public_map = _load(os.path.join(out, "data", "public", "maps", "open-demo.json"))
        first = public_cards["cards"][0]
        for needle in ("secret", "vault", "payroll"):
            self.assertNotIn(needle, (first["answer"] + first["explanation"]).lower())
        self.assertIn("[private project]", first["answer"])
        self.assertEqual(first["explanation"], "Borrowed from [private project]. Runs against portal [id] in project [id].")
        self.assertEqual(_load(os.path.join(out, "data", "public", "projects.json"))["projects"][0]["aliases"][0:1], ["open demo"])
        self.assertNotIn("7700123", json.dumps(_load(os.path.join(out, "data", "public", "projects.json"))))
        # a snippet is written by the card author, not checked against the code: it is gated like prose
        self.assertEqual(public_cards["cards"][1]["code_snippet"], "# [private project] style\nmain()")
        self.assertEqual(public_cards["cards"][1]["evidence_quote"], "def main() -> int:")
        self.assertEqual(public_cards["cards"][1]["see_also"], ["open-demo:30"])     # the link into the private deck is gone
        self.assertEqual([r["project"] for r in public_map["relations"]], ["open-thing"])
        blob = json.dumps([_load(os.path.join(out, "data", "public", name)) for name in ("projects.json", "inventory.json")]).lower()
        self.assertNotIn("vault", blob)
        self.assertNotIn("payroll", blob)
        # the full wording lives in the private tier only, and only the private edition reads it
        overlay = os.path.join(out, "data", "private", "unredacted", "open-demo.json")
        self.assertIn("Secret-Vault", _load(overlay)["cards"]["cards"][0]["answer"])
        # the owner's own edition keeps the project id but never the account number
        self.assertEqual(_load(overlay)["cards"]["cards"][0]["explanation"], "Borrowed from the vault thing. Runs against portal [id] in project acme-bq.")
        public = store.load_edition(out, ["public"], TODAY)
        self.assertNotIn("Vault", public["cards"][0]["answer"])
        self.assertEqual(public["edition"], "public")
        both = store.load_edition(out, ["public", "private"], TODAY)
        self.assertIn("Secret-Vault", both["cards"][0]["answer"])
        self.assertEqual(len([p for p in both["projects"] if p["slug"] == "open-demo"][0]["relations"]), 2)
        # a deck that stops naming private work loses its overlay on the next ingest
        _dump(dict(self.deck(), slug="open-demo"), os.path.join(work, "cards", "open-demo.json"))
        clean_map = _load(os.path.join(work, "maps", "open-demo.json"))
        clean_map["relations"] = []
        _dump(clean_map, os.path.join(work, "maps", "open-demo.json"))
        self.assertEqual(self._ingest(work, out, "open-demo")["filed"][0]["redactions"], 0)
        self.assertFalse(os.path.exists(overlay))

    def test_privacy_gate_and_bracket_paths(self):
        clean, found = store.privacy_gate({"answer": "see KP_Load and kp-load, not reload", "evidence_quote": "import KP_Load", "n": 3}, ["KP_Load", "abc"])
        self.assertEqual(clean["answer"], "see [private project] and [private project], not reload")
        self.assertEqual(clean["evidence_quote"], "import KP_Load")
        self.assertEqual(len(found), 1)
        same, found = store.privacy_gate({"answer": "nothing here"}, [])
        self.assertEqual((same, found), ({"answer": "nothing here"}, []))
        clean, _ = store.privacy_gate({"answer": "The KP_Load pipeline feeds the kp load mirror. KP-Load wins."}, ["KP_Load"])
        self.assertEqual(clean["answer"], "The [private project] pipeline feeds the [private project] mirror. [private project] wins.")
        # a name the public checkout spells out itself (a directory, a file, a line) is already public there
        os.makedirs(os.path.join(self.repo, "tx-load"))
        with open(os.path.join(self.repo, "tx-load", "PLAN.md"), "w") as fh:
            fh.write("# plan\nReplicates the tide_vault loader.\n")
        own = store.public_vocabulary(["TX-Load", "Tide-Vault", "Secret-Vault", "abc"], [self.repo])
        self.assertEqual(own, {"TX-Load", "Tide-Vault"})
        clean, found = store.privacy_gate({"question": "What does tx-load mirror, unlike Secret-Vault?",
                                           "relations": [{"project": "TX-Load", "relation": "successor"}]}, ["TX-Load", "Secret-Vault"], own)
        self.assertEqual(clean["question"], "What does tx-load mirror, unlike [private project]?")
        self.assertEqual(clean["relations"], [])      # a relation to a private project is dropped even when its name is public
        # account identifiers are scrubbed by exact string, outside verbatim fields; a nickname built on one is dropped
        clean, n = store.scrub_terms({"detail": "prod portal 7700123 (object p7700123_plan, not 17700123), sandbox 880045, acme-bqx.",
                                      "expected_output": "portal=7700123", "evidence_quote": "PORTAL = 7700123",
                                      "aliases": ["acme-bq deals", "the loader"], "n": 7700123}, ["7700123", "880045", "acme-bq"])
        self.assertEqual(clean["detail"], "prod portal [id] (object p[id]_plan, not 17700123), sandbox [id], acme-bqx.")
        self.assertEqual(clean["expected_output"], "portal=[id]")
        self.assertEqual(clean["evidence_quote"], "PORTAL = 7700123")       # a citation must keep matching the code
        self.assertEqual(clean["aliases"], ["the loader"])
        self.assertEqual((n, clean["n"]), (5, 7700123))
        clean, n = store.scrub_terms({"why": "Plan line for Borealis Ltd totals $4.2M. The bridge is open to anyone. Borealis rows stay."},
                                     [{"term": "Borealis Ltd", "as": "[account]"}, {"term": "Borealis", "as": "[account]"}, {"term": "$4.2M", "as": "[amount]"},
                                      {"term": "The bridge is open to anyone.", "as": ""}])
        self.assertEqual(clean["why"], "Plan line for [account] totals [amount]. [account] rows stay.")
        self.assertEqual(n, 4)
        pmap = {"architecture": [{"component": "skill", "path": ".", "responsibility": "[.claude/skills/SKILL.md] gate before writes"},
                                 {"component": "app", "path": "src/app.py", "responsibility": "[not/a/path] stays"},
                                 {"component": "ghost", "path": ".", "responsibility": "[missing/file.py] stays too"}]}
        self.assertEqual(store.promote_bracket_paths(pmap, [self.repo]), 1)
        self.assertEqual(pmap["architecture"][0], {"component": "skill", "path": ".claude/skills/SKILL.md", "responsibility": "gate before writes"})
        self.assertEqual(pmap["architecture"][2]["path"], ".")

    def test_bundle_is_script_safe_and_single_file_builds(self):
        edition = demo_edition()
        data = bundle.build_bundle(edition, generated_at="2026-11-12T00:00:00Z")
        self.assertEqual(data["edition"], "public")
        self.assertEqual(len(data["cards"]), len(edition["cards"]))
        literal = bundle.js_literal({"x": "</script><!--/BODY--><script>"})
        self.assertNotIn("<", literal)                                   # nothing can close the script or fake a marker
        self.assertEqual(json.loads(literal), {"x": "</script><!--/BODY--><script>"})
        html = bundle.inline_app(ROOT, data)
        self.assertNotIn('<script src="js/', html)
        self.assertNotIn('href="styles.css"', html)
        self.assertIn("window.RECURSER_BUNDLE", html)
        fragment = bundle.as_fragment(html)
        self.assertTrue(fragment.lstrip().startswith("<title>Recurser</title>"))
        for tag in ("<!doctype", "<html", "<head>", "<body>"):
            self.assertNotIn(tag, fragment.lower())

    def test_cross_links_resolve_inside_the_edition(self):
        edition = demo_edition()
        by_id = {c["id"]: c for c in edition["cards"]}
        self.assertEqual(by_id["tide-loader:00"]["see_also"], ["harbor-notes:00", "harbor-notes:60"])   # the unknown target is dropped
        self.assertEqual(by_id["harbor-notes:60"]["see_also"], ["tide-loader:00"])
        data = bundle.build_bundle(edition, generated_at="2026-11-12T00:00:00Z")
        self.assertEqual([c for c in data["cards"] if c["id"] == "tide-loader:00"][0]["see_also"], ["harbor-notes:00", "harbor-notes:60"])
        with tempfile.TemporaryDirectory() as tmp:
            kb.export_kb(edition, tmp)
            page = _text(os.path.join(tmp, "10-tide-loader.md"))
            self.assertIn("*See also:* 60·00 What harbor-notes is (harbor-notes); 60·60 Files, not a database (harbor-notes)", page)

    def test_in_memory_database(self):
        con = db.build(demo_edition())
        rows = db.query(con, "select code, card_count, tier from v_projects order by code")
        self.assertEqual([(r["code"], r["card_count"]) for r in rows], [("10", 13), ("60", 6)])
        self.assertEqual(db.query(con, "select count(*) n from cards where type = 'output'")[0]["n"], 3)
        self.assertGreater(db.query(con, "select count(*) n from facts where kind = 'decisions'")[0]["n"], 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.sqlite")
            db.dump(con, path)
            self.assertGreater(os.path.getsize(path), 1000)


class IngestRulesTests(RepoCase):
    """What ingest promises: fail closed, plan first, keep what is filed, one tier per project."""

    def stage(self, entries, decks=None):
        work = os.path.join(self.tmp.name, "work")
        os.makedirs(os.path.join(work, "maps"), exist_ok=True)
        os.makedirs(os.path.join(work, "cards"), exist_ok=True)
        base = _load(os.path.join(DEMO, "data", "public", "maps", "tide-loader.json"))
        projects = []
        for slug, visibility, extra in entries:
            pmap = dict(base, slug=slug, repo=slug, relations=[], aliases=[])
            for key in ("entry_points", "architecture", "key_functions", "infra", "decisions", "gotchas"):
                pmap[key] = [dict(it, **{k: "src/app.py" for k in ("path", "evidence") if k in it}) for it in pmap[key]]
            _dump(pmap, os.path.join(work, "maps", f"{slug}.json"))
            _dump(dict((decks or {}).get(slug) or self.deck(), slug=slug), os.path.join(work, "cards", f"{slug}.json"))
            entry = {"slug": slug, "repo": slug, "fork": False, "pushed_at": "2026-10-01T00:00:00Z", "dir": self.repo, "branch": "main", "alts": []}
            if visibility is not None:
                entry["visibility"] = visibility
            entry.update(extra or {})
            projects.append(entry)
        _dump({"as_of": "2026-11-12", "owner": "me", "projects": projects}, os.path.join(work, "projects.json"))
        return work

    def run_ingest(self, work, codes, snapshot=None, **kw):
        snap = None
        if snapshot is not None:
            snap = os.path.join(work, "snap.tsv")
            with open(snap, "w") as fh:
                fh.write(snapshot)
        return store.ingest(self.out, manifest_path=os.path.join(work, "projects.json"), maps_dir=os.path.join(work, "maps"),
                            cards_dir=os.path.join(work, "cards"), codes=codes, snapshot_path=snap, log=lambda *_: None, **kw)

    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.tmp.name, "out")

    def tier_files(self, tier, kind="cards"):
        path = os.path.join(self.out, "data", tier, kind)
        return sorted(os.listdir(path)) if os.path.isdir(path) else []

    def test_only_an_explicit_public_is_public(self):
        entries = [("a-upper", "PRIVATE", None), ("b-internal", "internal", None), ("c-missing", None, None), ("d-padded", " private ", None),
                   ("e-bool", True, None), ("f-open", " Public ", None)]
        work = self.stage(entries)
        codes = {slug: f"1{i}" for i, (slug, _, _) in enumerate(entries)}
        report = self.run_ingest(work, codes, snapshot="vault-one\t2026-01-01T00:00:00Z\tPRIVATE\t0\nvault-two\t2026-01-01T00:00:00Z\tinternal\t0\n"
                                                          "vault-three\t2026-01-01T00:00:00Z\tprivate \t0\nopen-one\t2026-01-01T00:00:00Z\tpublic\t0\n")
        self.assertEqual({f["slug"]: f["tier"] for f in report["filed"]},
                         {"a-upper": "private", "b-internal": "private", "c-missing": "private", "d-padded": "private", "e-bool": "private", "f-open": "public"})
        self.assertEqual(self.tier_files("public"), ["f-open.json"])
        self.assertEqual([r["name"] for r in _load(os.path.join(self.out, "data", "public", "inventory.json"))["repos"]], ["open-one"])
        self.assertEqual(inventory.normal_visibility({"private": False}), "public")
        self.assertEqual(inventory.normal_visibility({"isPrivate": True}), "private")
        self.assertEqual(inventory.normal_visibility({}), "private")
        with self.assertRaises(ValueError):
            store.load_edition(self.out, ["public", "private/"], TODAY)

    def test_no_checkout_no_filing_and_links_cannot_leave_it(self):
        work = self.stage([("ghost", "public", {"dir": os.path.join(self.tmp.name, "nowhere")}), ("real", "public", None)])
        report = self.run_ingest(work, {"ghost": "10", "real": "11"})
        self.assertEqual([f["slug"] for f in report["filed"]], ["real"])
        self.assertIn("checkout not found", report["skipped"][0]["why"])
        outside = os.path.join(self.tmp.name, "outside.txt")
        with open(outside, "w") as fh:
            fh.write("def main() -> int:\n")
        os.symlink(outside, os.path.join(self.repo, "link.txt"))
        os.symlink(self.tmp.name, os.path.join(self.repo, "up"))
        for ref in ("link.txt:1", "up/outside.txt"):
            full, why = schema.resolve_source(ref, [self.repo])
            self.assertIsNone(full, ref)
            self.assertIn("leaves the checkout", why)
        self.assertIsNone(schema.resolve_source("src/app.py:4", [self.repo])[0])    # three lines, no fourth
        self.assertTrue(schema.resolve_source("src/app.py:3", [self.repo])[0])

    def test_a_partial_run_keeps_what_is_filed_and_a_move_leaves_the_old_tier(self):
        work = self.stage([("alpha", "public", None), ("beta", "public", None)])
        self.run_ingest(work, {"alpha": "10", "beta": "11"})
        # a later run that only carries gamma: alpha and beta stay in the edition
        work = self.stage([("gamma", "private", None)])
        self.run_ingest(work, {"gamma": "12"})
        both = store.load_edition(self.out, None, TODAY)
        self.assertEqual([p["slug"] for p in both["projects"]], ["alpha", "beta", "gamma"])
        # beta turns private: its public files and manifest entry go away
        work = self.stage([("beta", "private", None)])
        report = self.run_ingest(work, {"beta": "11"})
        self.assertEqual(report["moved"], [{"slug": "beta", "from": "public", "to": "private"}])
        self.assertEqual(self.tier_files("public"), ["alpha.json"])
        self.assertEqual(self.tier_files("public", "maps"), ["alpha.json"])
        self.assertEqual(self.tier_files("private"), ["beta.json", "gamma.json"])
        self.assertEqual([p["slug"] for p in store.load_edition(self.out, ["public"], TODAY)["projects"]], ["alpha"])
        self.assertEqual(store.load_edition(self.out, ["public"], TODAY)["edition"], "public")
        # a refreshed deck that fails validation keeps its last good version filed
        bad = self.deck(good_card(evidence_quote="this sentence is not in the file"))
        work = self.stage([("alpha", "public", None)], {"alpha": bad})
        report = self.run_ingest(work, {"alpha": "10"})
        self.assertEqual(report["filed"], [])
        self.assertEqual([p["slug"] for p in store.load_edition(self.out, None, TODAY)["projects"]], ["alpha", "beta", "gamma"])

    def test_a_late_failure_writes_nothing(self):
        work = self.stage([("alpha", "public", None)])
        self.run_ingest(work, {"alpha": "10"})
        before = _text(os.path.join(self.out, "data", "public", "cards", "alpha.json"))
        changed = self.deck(good_card(answer="python -m demo --changed"))
        work = self.stage([("alpha", "public", None), ("gamma", "private", None)], {"alpha": changed})
        with self.assertRaises(address.RegistryError):
            self.run_ingest(work, {"alpha": "10", "gamma": "10"})          # duplicate code
        self.assertEqual(_text(os.path.join(self.out, "data", "public", "cards", "alpha.json")), before)
        self.assertEqual(self.tier_files("private"), [])

    def test_redaction_that_breaks_the_contract_is_refused(self):
        long_concept = ("entry point shared with vault " * 3).strip()[:76]            # 76 chars, close to the limit of 80
        self.assertIn("vault", long_concept)
        tight = self.deck(good_card(concept=long_concept))
        work = self.stage([("open-demo", "public", None), ("vault", "private", None)], {"open-demo": tight})
        report = self.run_ingest(work, {"open-demo": "50", "vault": "51"})
        self.assertEqual([f["slug"] for f in report["filed"]], ["vault"])
        self.assertIn("no longer fits the contract", report["skipped"][0]["why"])
        self.assertEqual(self.tier_files("public"), [])

    def test_names_glued_to_a_longer_name_are_not_public_vocabulary(self):
        with open(os.path.join(self.repo, "src", "notes.md"), "w") as fh:
            fh.write("deployed as tx-load-bridge next to pre_kp_load\n")
        self.assertEqual(store.public_vocabulary(["TX-Load", "KP_Load"], [self.repo]), set())
        clean, _ = store.privacy_gate({"detail": "runs as tx-load-bridge"}, ["TX-Load"])
        self.assertEqual(clean["detail"], "runs as [private project]-bridge")       # the gate itself stays loose

    def test_an_edited_public_deck_wins_over_its_stale_full_wording(self):
        leaky = self.deck(good_card(answer="python -m demo, as in vault-core"))
        work = self.stage([("open-demo", "public", None), ("vault-core", "private", None)], {"open-demo": leaky})
        self.run_ingest(work, {"open-demo": "50", "vault-core": "51"})
        both = store.load_edition(self.out, None, TODAY)
        self.assertIn("vault-core", [c for c in both["cards"] if c["id"] == "open-demo:30"][0]["answer"])
        self.assertEqual(both["warnings"], [])
        path = os.path.join(self.out, "data", "public", "cards", "open-demo.json")
        deck = _load(path)
        deck["cards"][0]["answer"] = "python -m demo --fixed"
        _dump(deck, path)
        both = store.load_edition(self.out, None, TODAY)
        self.assertEqual([c for c in both["cards"] if c["id"] == "open-demo:30"][0]["answer"], "python -m demo --fixed")
        self.assertEqual(len(both["warnings"]), 1)

    def test_unchecked_projects_keep_their_stale_marks(self):
        work = self.stage([("alpha", "public", None), ("beta", "public", None)])
        self.run_ingest(work, {"alpha": "10", "beta": "11"})
        sources = os.path.join(self.tmp.name, "fresh")
        for slug in ("alpha", "beta"):
            os.makedirs(os.path.join(sources, slug, "src"))
            with open(os.path.join(sources, slug, "src", "app.py"), "w") as fh:
                fh.write("import sys\n\ndef start():\n    return 0\n")
        store.write_stale(self.out, store.find_stale(self.out, sources))
        self.assertEqual(sorted(_load(os.path.join(self.out, "data", "public", "stale.json"))), ["alpha", "beta"])
        os.rename(os.path.join(sources, "beta"), os.path.join(sources, "beta-away"))
        report = store.find_stale(self.out, sources)
        self.assertEqual(report["unchecked"], ["beta"])
        store.write_stale(self.out, report)
        self.assertEqual(sorted(_load(os.path.join(self.out, "data", "public", "stale.json"))), ["alpha", "beta"])


class GuardTests(unittest.TestCase):
    def test_a_private_edition_is_refused_where_git_would_publish_it(self):
        import argparse
        from recurser import cli

        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "checkout")
            os.makedirs(os.path.join(root, "docs"))
            subprocess.run(["git", "init", "-q", root], check=True)
            args = argparse.Namespace(root=root)
            inside, docs = os.path.join(root, "out.xlsx"), os.path.join(root, "docs", "kb")
            ignored, outside = os.path.join(root, "dist", "recurser.html"), os.path.join(tmp, "elsewhere.html")
            # a checkout that commits data/private (the owner's private repository): only docs/ is off limits
            cli._refuse_private_output(args, True, inside, ignored, outside)
            with self.assertRaises(SystemExit):
                cli._refuse_private_output(args, True, docs)
            # the public checkout keeps data/private out of git: nothing private may land in a tracked place
            with open(os.path.join(root, ".gitignore"), "w") as fh:
                fh.write("data/private/\ndist/\n")
            cli._refuse_private_output(args, True, ignored, outside, None)
            for path in (inside, docs, os.path.join(root, "app", "recurser.html")):
                with self.assertRaises(SystemExit):
                    cli._refuse_private_output(args, True, path)
            cli._refuse_private_output(args, False, inside, docs)        # a public edition goes anywhere
            with self.assertRaises(SystemExit):
                cli._tiers(argparse.Namespace(tiers="public,private/"))
            self.assertEqual(cli._tiers(argparse.Namespace(tiers="public, private")), ["public", "private"])


class ExportTests(unittest.TestCase):
    def test_anki_files(self):
        edition = demo_edition()
        with tempfile.TemporaryDirectory() as tmp:
            paths = anki.export_all(edition, tmp, apkg=False)
            classic = _text(os.path.join(tmp, "export_for_anki", "10-tide-loader.csv"), "utf-8-sig").splitlines()
            self.assertTrue(classic[0].startswith("1000;What tide-loader is;"))
            cards = _text(os.path.join(tmp, "recurser-cards.txt")).splitlines()
            self.assertEqual(cards[2], "#notetype:Recurser Card v1")
            body = [line for line in cards if not line.startswith("#")]
            self.assertTrue(all(len(line.split("\t")) == len(anki.CARD_FIELDS) + 3 for line in body))
            steps = [line for line in _text(os.path.join(tmp, "recurser-steps.txt")).splitlines() if not line.startswith("#")]
            self.assertEqual(len(steps), 10)
            self.assertIn("[Beginning]", steps[0])
            self.assertEqual(len(paths), 4)
            with tempfile.TemporaryDirectory() as again:
                anki.export_all(edition, again, apkg=False)
                self.assertEqual(_text(os.path.join(again, "recurser-cards.txt")), "\n".join(cards) + "\n")   # deterministic

    def test_anki_inline_code(self):
        tick = chr(96)
        self.assertEqual(anki.rich(f"call {tick}load(<x>){tick} then {tick}merge(){tick}"), "call <code>load(&lt;x&gt;)</code> then <code>merge()</code>")
        self.assertEqual(anki.rich(f"an odd {tick} tick"), f"an odd {tick} tick")      # unbalanced: left as typed
        self.assertEqual(anki.rich("plain <b>"), "plain &lt;b&gt;")

    def test_anki_only_learned_and_set_aside(self):
        edition = demo_edition()
        progress = {"cards": {"tide-loader:00": {"learned": 1}, "tide-loader:10": {"learned": 1}}, "flags": {"tide-loader:10": "suspended"}}
        self.assertEqual([c["id"] for c in anki.select_cards(edition, progress, only_learned=True)], ["tide-loader:00"])

    def test_kb_pages(self):
        edition = demo_edition()
        with tempfile.TemporaryDirectory() as tmp:
            paths = kb.export_kb(edition, tmp, progress={"gaps": {"tide-loader:00": {"t": 1, "text": "forgot idempotent"}}})
            self.assertEqual(len(paths), 3)
            page = _text(os.path.join(tmp, "10-tide-loader.md"))
            self.assertIn("<!-- recurser:tide-loader:00 -->", page)
            self.assertIn("Last gap:* forgot idempotent", page)
            self.assertIn("▢ **trap I hit**", page)

    def test_workbook_builds_and_keeps_fillins(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl is not installed")
        from recurser import xlsx

        edition = demo_edition()
        with tempfile.TemporaryDirectory() as tmp:
            first = xlsx.build_workbook(edition, os.path.join(tmp, "a.xlsx"), progress={"cards": {"tide-loader:00": {"learned": 1}}, "log": [{"id": "tide-loader:00", "t": 1, "r": 2}]})
            wb = openpyxl.load_workbook(first)
            self.assertEqual(wb.sheetnames, ["instructions", "overview", "matrix", "sit", "drill", "traces", "taxonomy", "index"])
            mx = wb["matrix"]
            self.assertEqual(mx["A2"].value, 1)
            self.assertEqual(mx["A22"].value, "=SUM(A2:A20)")
            mx["I3"] = "my own wording"
            wb["sit"]["F6"] = "an attempt"
            wb.save(first)
            second = xlsx.build_workbook(edition, os.path.join(tmp, "b.xlsx"), keep_from=first)
            wb2 = openpyxl.load_workbook(second)
            self.assertEqual(wb2["matrix"]["I3"].value, "my own wording")
            self.assertEqual(wb2["matrix"]["A2"].value, 1)
            self.assertEqual(wb2["sit"]["F6"].value, "an attempt")
            open(os.path.join(tmp, "~$c.xlsx"), "w").close()
            with self.assertRaises(xlsx.WorkbookLocked):
                xlsx.build_workbook(edition, os.path.join(tmp, "c.xlsx"))

    def test_formula_like_text_stays_text(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl is not installed")
        from recurser import xlsx

        edition = demo_edition()
        edition["cards"][0] = dict(edition["cards"][0], answer="=SUM(1,2) is what the cell holds")
        with tempfile.TemporaryDirectory() as tmp:
            path = xlsx.build_workbook(edition, os.path.join(tmp, "f.xlsx"))
            cell = openpyxl.load_workbook(path)["matrix"]["E2"]
            self.assertEqual(cell.data_type, "s")
            self.assertTrue(cell.value.startswith("=SUM(1,2) is"))


class ServerTests(unittest.TestCase):
    def serve(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        httpd, token = server.create_server(os.path.join(ROOT, "app"), port=0, progress_path=os.path.join(tmp.name, "p.json"), info={"edition": "public"}, **kw)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}", token

    def call(self, url, data=None, headers=None):
        req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, method="PUT" if data is not None else "GET", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def test_static_files_health_and_traversal(self):
        base, _ = self.serve()
        status, body, _ = self.call(base + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>Recurser</title>", body)
        self.assertEqual(json.loads(self.call(base + "/api/health")[1])["ok"], True)
        self.assertEqual(self.call(base + "/../recurser/cli.py")[0], 404)
        self.assertEqual(self.call(base + "/%2e%2e/README.md")[0], 404)

    def test_progress_merges_per_card(self):
        base, _ = self.serve()
        first = {"updated": 10, "cards": {"a:00": {"last": 10, "reps": 1}, "b:00": {"last": 5, "reps": 1}}, "log": [{"id": "a:00", "t": 10, "r": 2}], "days": {"2026-11-12": {"n": 2, "ok": 1, "fresh": 2}}}
        self.assertEqual(self.call(base + "/api/progress", first)[0], 200)
        second = {"updated": 20, "cards": {"a:00": {"last": 4, "reps": 9}, "c:00": {"last": 20, "reps": 1}}, "log": [{"id": "c:00", "t": 20, "r": 0}], "gaps": {"c:00": {"t": 20, "text": "missed it"}}}
        merged = json.loads(self.call(base + "/api/progress", second)[1])
        self.assertEqual(merged["cards"]["a:00"]["reps"], 1)            # the more recent review wins, whichever file it came from
        self.assertEqual(set(merged["cards"]), {"a:00", "b:00", "c:00"})
        self.assertEqual(len(merged["log"]), 2)
        self.assertEqual(merged["days"]["2026-11-12"]["n"], 2)
        self.assertEqual(merged["gaps"]["c:00"]["text"], "missed it")
        self.assertEqual(json.loads(self.call(base + "/api/progress")[1])["updated"], 20)
        self.assertEqual(self.call(base + "/api/progress", ["not", "an", "object"])[0], 400)
        self.assertEqual(self.call(base + "/api/progress", first, {"Origin": "http://evil.example"})[0], 403)

    def test_token_is_required_beyond_loopback(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        handler = server.make_handler(os.path.join(ROOT, "app"), server.ProgressFile(os.path.join(tmp.name, "p.json")), "s3cret-token", {})
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.assertEqual(self.call(base + "/")[0], 401)
        self.assertEqual(self.call(base + "/api/progress")[0], 401)
        status, _, headers = self.call(base + "/?token=s3cret-token")
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.call(base + "/api/health", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.call(base + "/?token=wrong")[0], 401)
        self.assertFalse(server.is_loopback("0.0.0.0"))


if __name__ == "__main__":
    unittest.main()
