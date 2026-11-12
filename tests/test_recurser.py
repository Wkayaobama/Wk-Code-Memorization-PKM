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
import urllib.parse
import urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(1, os.path.join(ROOT, "tests"))      # the stand-in servers, however the tests are started

from recurser import address, anki, assistant, bundle, config, db, fuzzy, inventory, k8s, kb, operator, retrieval, schema, server, srs, store  # noqa: E402

try:
    import anthropic
except ImportError:  # the assistant tests need the SDK
    anthropic = None
try:
    import kubernetes
except ImportError:  # the tests that drive the official client need it; the others do not
    kubernetes = None

DEMO = os.path.join(ROOT, "tests", "fixtures", "demo")
DEMO_LINK = "demo-" + "link-0123456789"      # a made-up sign-in value for the server tests
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
        httpd, token = server.create_server(os.path.join(ROOT, "app"), port=0, progress_path=os.path.join(tmp.name, "p.json"), edition=demo_edition(), **kw)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}", token

    def call(self, url, data=None, headers=None, method=None):
        req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                     method=method or ("PUT" if data is not None else "GET"), headers=headers or {})
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
        handler = server.make_handler(os.path.join(ROOT, "app"), server.Site(demo_edition(), progress_path=os.path.join(tmp.name, "p.json"), token=DEMO_LINK))
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.assertEqual(self.call(base + "/")[0], 401)
        self.assertEqual(self.call(base + "/api/progress")[0], 401)
        status, _, headers = self.call(base + "/?token=" + DEMO_LINK)
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.call(base + "/api/health", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.call(base + "/?token=wrong")[0], 401)
        self.assertFalse(server.is_loopback("0.0.0.0"))


class AudienceTests(unittest.TestCase):
    def test_an_edition_for_chosen_projects(self):
        edition = demo_edition()
        self.assertEqual(store.resolve_projects(edition, ["Tide-Loader", "60"]), ["tide-loader", "harbor-notes"])
        with self.assertRaises(KeyError):
            store.resolve_projects(edition, ["tide"])                       # no guessing: a near miss is refused
        team = store.restrict(edition, ["tide-loader"])
        self.assertEqual([p["slug"] for p in team["projects"]], ["tide-loader"])
        self.assertEqual({c["slug"] for c in team["cards"]}, {"tide-loader"})
        self.assertEqual({r["name"] for r in team["inventory"]}, {"tide-loader"})
        self.assertTrue(all("see_also" not in c for c in team["cards"]))        # links into the other deck are gone
        self.assertEqual(len(edition["projects"]), 2)                           # the full edition is untouched
        with self.assertRaises(KeyError):
            store.restrict(edition, ["nope"])

    def test_names_of_private_projects_outside_the_selection_are_removed(self):
        edition = demo_edition()
        edition = {**edition, "projects": [dict(p, visibility="private" if p["slug"] == "harbor-notes" else "public") for p in edition["projects"]]}
        team = store.restrict(edition, ["tide-loader"])
        first = [c for c in team["cards"] if c["id"] == "tide-loader:00"][0]
        self.assertNotIn("harbor-notes", first["confusables"])
        self.assertIn("[private project]", first["confusables"])
        self.assertEqual((first["id"], first["slug"]), ("tide-loader:00", "tide-loader"))

    def test_an_audience_page_hides_names_in_quotes_paths_and_branches_too(self):
        edition = demo_edition()
        projects = [dict(p, visibility="private" if p["slug"] == "harbor-notes" else "public") for p in edition["projects"]]
        tide = [p for p in projects if p["slug"] == "tide-loader"][0]
        tide.update(branch="claude/harbor-notes-bridge-x1", commit="abc123", alts=[{"branch": "feat/harbor_notes", "note": "", "commit": "def456"}])
        cards = [dict(c) for c in edition["cards"]]
        first = [c for c in cards if c["id"] == "tide-loader:00"][0]
        first.update(evidence_quote="import harbor-notes as hn", source_refs=[{"path": "vendor/harbor-notes/x.py", "line": 3, "branch": "claude/harbor-notes-bridge-x1", "commit": "abc123"}])
        inventory = edition["inventory"] + [{"name": "ab", "visibility": "private", "slug": None}]
        team = store.restrict({**edition, "projects": projects, "cards": cards, "inventory": inventory}, ["tide-loader"])
        blob = json.dumps({"projects": team["projects"], "cards": team["cards"]})
        self.assertNotIn("harbor-notes", blob)
        self.assertNotIn("harbor_notes", blob)
        self.assertEqual(team["projects"][0]["commit"], "abc123")                # the commit still links to the code
        self.assertEqual(team["cards"][0]["source_refs"][0]["commit"], "abc123")
        self.assertEqual(team["unmatched_private_names"], ["ab"])                # too short to hide: the caller must refuse


class RetrievalTests(unittest.TestCase):
    def test_a_question_finds_its_card_inside_the_audience_only(self):
        edition = demo_edition()
        index = retrieval.Index(edition)
        hits = index.search("why does tide-loader merge on a key instead of appending?", k=5)
        self.assertTrue(hits and all(h["slug"] == "tide-loader" for h in hits[:2]))
        self.assertIn("card", {h["kind"] for h in hits})
        self.assertTrue(all(h["blocks"] and h["source"].startswith("recurser://") for h in hits))
        self.assertEqual({h["slug"] for h in index.search("markdown file per note index", k=6, slugs=["harbor-notes"])}, {"harbor-notes"})
        self.assertEqual(index.search("the of and", k=5), [])                     # nothing but stop words
        only_tide = retrieval.Index(store.restrict(edition, ["tide-loader"]))
        self.assertTrue(all(h["slug"] == "tide-loader" for h in only_tide.search("markdown file per note index", k=8)))
        self.assertEqual(retrieval.tokens("loadStation(BRE)"), ["loadstat", "load", "station", "bre"])     # the joined word is stemmed, the parts kept

    def test_words_are_matched_by_their_stem_and_names_in_any_spelling(self):
        for group in (("cache", "caches", "cached", "caching"), ("authenticate", "authenticating", "authentication"),
                      ("tokenize", "tokenization"), ("load", "loads", "loading", "loaded"), ("query", "queries")):
            self.assertEqual(len({retrieval.stem(w) for w in group}), 1, group)
        self.assertEqual((retrieval.stem("station"), retrieval.stem("class"), retrieval.stem("run"), retrieval.stem("running")), ("station", "class", "run", "run"))
        self.assertEqual(retrieval.tokens("répétition"), retrieval.tokens("repetition"))
        edition = demo_edition()
        edition = {**edition, "projects": [dict(p, aliases=["the TideGauge Loader"] if p["slug"] == "tide-loader" else []) for p in edition["projects"]]}
        index = retrieval.Index(edition)
        named = lambda q: index.named_projects(set(retrieval.tokens(q, query=True)))              # noqa: E731
        self.assertEqual(named("what does the tidegauge loader do"), {"tide-loader"})            # lower case
        self.assertEqual(named("what does the tide gauge loader do"), {"tide-loader"})           # spelled apart
        self.assertEqual(named("is the TideGauge thing fast"), set())                            # a name needs all its words
        self.assertTrue(index.search("loading stations", k=3))                                    # inflected words still find their cards

    def test_the_per_project_limit_spares_a_single_or_named_project(self):
        edition = demo_edition()
        only = retrieval.Index(store.restrict(edition, ["tide-loader"]))
        self.assertEqual(len(only.search("tide loader station batch merge receipt load file", k=8, per_project=2)), 8)
        index = retrieval.Index(edition)
        hits = index.search("how does tide-loader load a station batch and merge the receipt", k=8, per_project=2)
        self.assertGreater(sum(h["slug"] == "tide-loader" for h in hits), 2)                      # named in the question: not limited
        open_hits = index.search("file per note station batch merge index markdown", k=8, per_project=2)
        self.assertEqual(len(open_hits), 8)                                                       # set-aside passages fill the list again
        self.assertEqual(open_hits, sorted(open_hits, key=lambda h: -h["score"]))

    def test_settings_file(self):
        merged = config.merge({"llm": {"effort": "high"}, "access": {"only": ["tide-loader"]}})
        self.assertEqual((merged["llm"]["model"], merged["llm"]["effort"], merged["server"]["port"]), ("claude-opus-5-5", "high", 8000))
        for bad in ({"llm": {"modle": "x"}}, {"llm": {"provider": "other"}}, {"server": {"port": "8000"}}, {"nope": {}}, {"llm": {"effort": "extreme"}},
                    {"llm": {"max_tokens": 512}}, {"llm": {"model": " "}}, {"access": {"trust_user_header": "X-User"}},
                    {"access": {"trust_user_header": "X-User", "trusted_proxies": ["not-an-address"]}},
                    {"llm": {"answer_seconds": 5}}, {"llm": {"timeout_seconds": 1}}):
            with self.assertRaises(config.ConfigError):
                config.merge(bad)
        self.assertEqual(config.merge({"llm": {"timeout_seconds": 120}})["llm"]["answer_seconds"], 180)      # the two limits are independent
        self.assertEqual(config.merge({"access": {"trust_user_header": "X-User", "trusted_proxies": ["10.42.0.0/16"]}})["access"]["trusted_proxies"], ["10.42.0.0/16"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            with open(path, "w") as fh:
                fh.write("llm:\n  effort: low\nretrieval:\n  top_k: 5\n")
            loaded = config.load(path)
            self.assertEqual((loaded["llm"]["effort"], loaded["retrieval"]["top_k"]), ("low", 5))


@unittest.skipIf(anthropic is None, "the anthropic package is not installed")
class AssistantTests(unittest.TestCase):
    """The SDK calls, run against a local stand-in for the API that speaks its streaming protocol."""

    def setUp(self):
        from fake_anthropic import FakeAnthropic

        self.api = FakeAnthropic()
        self.addCleanup(self.api.close)
        self.client = anthropic.Anthropic(api_key="test-key", base_url=self.api.url, max_retries=0)
        self.addCleanup(self.client.close)               # its connections stay open between questions, as with the real API
        self.passages = retrieval.Index(demo_edition()).search("what does tide-loader do?", k=3)

    def test_ask_sends_search_results_and_reads_citations(self):
        result = assistant.ask("What does tide-loader do?", self.passages, client=self.client)
        sent = self.api.requests[-1]
        body = sent["body"]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["thinking"], {"type": "adaptive", "display": "summarized"})       # so that thinking carries events
        self.assertEqual(body["output_config"], {"effort": "medium"})
        self.assertEqual(body["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", sent["headers"]["anthropic-beta"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_tokens"], 16000)
        content = body["messages"][0]["content"]
        self.assertEqual([b["type"] for b in content], ["search_result"] * 3 + ["text"])
        self.assertTrue(all(b["citations"] == {"enabled": True} and b["content"] and b["source"].startswith("recurser://") for b in content[:3]))
        self.assertEqual(content[-1]["text"], "What does tide-loader do?")
        self.assertNotIn("temperature", body)
        self.assertEqual(result["text"], "Grounded sentence. A closing remark.")
        self.assertEqual(result["segments"], [{"text": "Grounded sentence.", "cites": [self.passages[0]["id"]]}, {"text": " A closing remark.", "cites": []}])
        self.assertEqual(result["sources"], [self.passages[0]["id"]])
        self.assertEqual((result["refused"], result["truncated"], result["usage"]["output_tokens"]), (False, False, 42))

    def test_refusals_errors_and_empty_retrieval(self):
        self.assertEqual(assistant.ask("anything", [], client=self.client)["sources"], [])
        self.assertEqual(self.api.requests, [])                               # nothing retrieved: nothing is sent
        self.api.mode = "refusal"
        self.assertTrue(assistant.ask("q", self.passages, client=self.client)["refused"])
        for mode, status in (("status:429", 429), ("status:401", 503), ("status:404", 503), ("status:500", 502)):
            self.api.mode = mode
            with self.assertRaises(assistant.AssistantError) as caught:
                assistant.ask("q", self.passages, client=self.client)
            self.assertEqual(caught.exception.status, status, mode)
        self.assertEqual(caught.exception.retry_after, None)
        self.api.mode = "status:429"
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant.ask("q", self.passages, client=self.client)
        self.assertEqual(caught.exception.retry_after, 7)
        unreachable = anthropic.Anthropic(api_key="k", base_url="http://127.0.0.1:9", max_retries=0, timeout=2.0)
        self.addCleanup(unreachable.close)
        with self.assertRaises(assistant.AssistantError) as caught:           # nothing listens there: a network failure, not a crash
            assistant.ask("q", self.passages, client=unreachable)
        self.assertIn(caught.exception.status, (502, 504))

    def test_failures_in_the_middle_of_an_answer_are_answered_not_raised(self):
        for mode, status in (("midstream_error", 503), ("cut", 502), ("thinking_only", 500)):
            self.api.mode = mode
            with self.assertRaises(assistant.AssistantError) as caught:
                assistant.ask("q", self.passages, client=self.client)
            self.assertEqual(caught.exception.status, status, mode)
        self.assertIn("max_tokens", str(caught.exception))
        self.api.mode = "midstream_error"
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant.critique({"slug": "x", "question": "q", "answer": "a"}, {}, "typed", client=self.client)
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (503, 20))
        self.api.mode = "pause"                                               # a stop reason this code does not know: not a full answer
        self.assertTrue(assistant.ask("q", self.passages, client=self.client)["truncated"])
        self.api.mode = "answer"
        with self.assertRaises(assistant.AssistantError) as caught:           # the whole answer has a deadline
            assistant.ask("q", self.passages, client=self.client, answer_seconds=-1)
        self.assertEqual(caught.exception.status, 504)

    def settle(self, seconds=10):
        """Wait for the threads that still read an abandoned answer: each ends by itself at its next event."""
        for thread in threading.enumerate():
            if thread.name == "recurser-answer":
                thread.join(seconds)
        self.assertFalse([t for t in threading.enumerate() if t.name == "recurser-answer"], "a thread that reads an answer is still running")

    def test_the_person_asking_waits_no_longer_than_the_deadline(self):
        import time

        from fake_anthropic import expected_text

        cases = {"keep-alives every 0.1 s": "[[id=A; ping:20:0.1; end]]", "keep-alives every 0.8 s": "[[id=A; ping:3:0.8; end]]",
                 "one event, then silence": "[[id=A; silence:2; end]]", "text that does not end in time": "[[id=A; text:20:0.1; end]]",
                 "an API that says nothing at all": "[[id=A; mute:2]]"}
        for label, plan in cases.items():
            begun = time.monotonic()
            with self.assertRaises(assistant.AssistantError) as caught:
                assistant.ask("Which key does the loader merge on? " + plan, self.passages, client=self.client, answer_seconds=0.5)
            took = time.monotonic() - begun
            self.assertEqual(caught.exception.status, 504, label)
            self.assertIn("took too long", str(caught.exception))
            self.assertLess(abs(took - 0.5), 0.3, f"{label}: told after {took:.2f} s")
        self.settle()
        self.assertTrue(self.api.hung_up)                # the threads left behind closed their streams themselves
        # an answer that fits in the time is returned whole, on the same client
        answer = assistant.ask("Which key? [[id=B; ping:3:0.1; text:4:0.05; end]]", self.passages, client=self.client, answer_seconds=20)
        self.assertEqual((answer["text"], answer["truncated"]), (expected_text("B", 4), False))
        self.settle()

    def test_an_abandoned_answer_never_disturbs_another_one(self):
        """Two people, one client, as in the server.  Over TLS when the openssl command is there to make a
        certificate: that is where reading a connection another thread has closed returns somebody else's bytes."""
        import ssl
        import time

        from fake_anthropic import FakeAnthropic, expected_text, make_certificate

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        pair = make_certificate(folder.name)
        if pair:
            api = FakeAnthropic(tls=pair)
            self.addCleanup(api.close)
            client = anthropic.Anthropic(api_key="test-key", base_url=api.url, max_retries=0,
                                         http_client=anthropic.DefaultHttpxClient(verify=ssl.create_default_context(cafile=pair[0]), trust_env=False))
            self.addCleanup(client.close)
        else:
            api, client = self.api, self.client
        card, results = {"slug": "tide-loader", "question": "q", "answer": "a"}, {}

        def ask(name, plan, limit):
            try:
                results[name] = assistant.critique(card, {}, "typed " + plan, client=client, answer_seconds=limit)["text"]
            except assistant.AssistantError as exc:
                results[name] = exc.status

        for n in range(4):
            left = threading.Thread(target=ask, args=(f"A{n}", f"[[id=A{n}; ping:10:0.1; end]]", 0.4))
            left.start()
            time.sleep(0.5)                              # A has just been given up; its thread still reads keep-alives
            other = threading.Thread(target=ask, args=(f"B{n}", f"[[id=B{n}; burst:3000; end]]", 30))
            other.start()
            left.join(10)
            other.join(40)
            self.assertEqual(results[f"A{n}"], 504)
            self.assertEqual(results[f"B{n}"], expected_text(f"B{n}", 3000).strip(), f"round {n}: B did not get its own complete answer")
        self.settle()
        self.assertEqual(api.url.startswith("https"), bool(pair))

    def test_an_answer_nobody_waits_for_keeps_its_place_until_it_ends(self):
        import time

        given_back = []
        slot = assistant.Slot(lambda: given_back.append(time.monotonic()))
        begun = time.monotonic()
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant.ask("Which key? [[id=A; ping:12:0.1; end]]", self.passages, client=self.client, answer_seconds=0.3, slot=slot)
        slot.caller_done()                               # what the server does when the call has returned
        self.assertEqual(caught.exception.status, 504)
        self.assertEqual(given_back, [])                 # the API is still writing: the place is still taken
        self.settle()
        self.assertEqual(len(given_back), 1)             # given back once, when the stream had ended
        self.assertGreater(given_back[0] - begun, 1.0)
        # an answer that ends in time gives its place back once; so does a call that never reaches the API
        for question, passages in (("Which key? [[id=B; text:2:0.01; end]]", self.passages), ("anything at all", [])):
            given_back.clear()
            slot = assistant.Slot(lambda: given_back.append(1))
            assistant.ask(question, passages, client=self.client, slot=slot)
            slot.caller_done()
            self.settle()
            slot.caller_done()                           # asking twice changes nothing
            self.assertEqual(given_back, [1], question)

    def test_odd_failures_are_answers_too(self):
        question = "Which key does the loader merge on?"
        self.api.mode = "error_text"                     # an error event whose "error" is a sentence, not an object
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant.ask(question, self.passages, client=self.client)
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (503, 20))
        self.api.mode = "garbage"                        # an event that is not JSON: whatever the SDK raises is reported, not raised
        with self.assertRaises(assistant.AssistantError) as caught:
            assistant.ask(question, self.passages, client=self.client)
        self.assertEqual(caught.exception.status, 502)
        self.assertIn("could not be read", str(caught.exception))
        # a profile or federation setting that cannot yield a token only fails when a question is sent
        failure = assistant._failure(anthropic.CredentialsError("Config file not found: no such profile"), "claude-opus-5-5")
        self.assertEqual(failure.status, 503)
        self.assertIn("credential could not be loaded: Config file not found", str(failure))
        self.settle()

    def test_a_credential_that_cannot_be_loaded_turns_the_assistant_off(self):
        saved = {k: os.environ.pop(k, None) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID")}
        try:
            for env in ({}, {"ANTHROPIC_API_KEY": "   "}, {"ANTHROPIC_API_KEY": "abc def"}, {"ANTHROPIC_API_KEY": "abcdef\n"},
                        {"ANTHROPIC_PROFILE": "no-such-profile-here"},
                        {"ANTHROPIC_FEDERATION_RULE_ID": "rule_without_the_rest"}):       # builds a client that could send nothing
                os.environ.update(env)
                with self.assertRaises(assistant.AssistantUnavailable, msg=str(env)):
                    assistant.make_client()
                for k in env:
                    del os.environ[k]
            os.environ["ANTHROPIC_API_KEY"] = "placeholder-value-for-the-test"
            self.assertEqual(assistant.make_client(timeout=7.0).max_retries, 1)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.update({k: v for k, v in saved.items() if v is not None})

    def test_critique_compares_with_the_reference(self):
        edition = demo_edition()
        card = [c for c in edition["cards"] if c["id"] == "tide-loader:00"][0]
        project = [p for p in edition["projects"] if p["slug"] == "tide-loader"][0]
        result = assistant.critique(card, project, "it loads tide files", client=self.client, fallbacks=False)
        body = self.api.requests[-1]["body"]
        text = body["messages"][0]["content"][0]["text"]
        self.assertIn("REFERENCE ANSWER: " + card["answer"], text)
        self.assertIn("it loads tide files", text)
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertNotIn("fallbacks", body)
        self.assertTrue(result["text"].startswith("partly."))


class TeamServerTests(ServerTests):
    USERS = [dict(name="owner", token="owner-" + "token-0123456789abcdef", projects="*"),          # demo values, not credentials
             dict(name="sam", token="sam-" + "token-0123456789abcdefgh", projects=["tide-loader"])]

    def team(self, client=None, settings=None, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        site = server.Site(demo_edition(), users=[dict(u) for u in self.USERS], progress_dir=tmp.name, client=client,
                           settings=config.merge(settings), **kw)
        self.site = site
        httpd, _ = server.create_server(os.path.join(ROOT, "app"), port=0, site=site)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"http://127.0.0.1:{httpd.server_address[1]}", tmp.name

    def cookie(self, base, user):
        status, _, headers = self.call(base + "/?token=" + user["token"])
        self.assertEqual(status, 200)
        return {"Cookie": headers["Set-Cookie"].split(";")[0]}

    def test_each_person_signs_in_and_sees_only_their_projects(self):
        base, folder = self.team()
        self.assertEqual(self.call(base + "/")[0], 401)
        self.assertEqual(self.call(base + "/data/bundle.js")[0], 401)
        self.assertEqual(self.call(base + "/?token=" + "x" * 28)[0], 401)
        self.assertEqual(self.call(base + "/healthz")[:2], (200, b"ok\n"))            # probes need no sign-in and get no data
        owner, sam = self.cookie(base, self.USERS[0]), self.cookie(base, self.USERS[1])
        info = json.loads(self.call(base + "/api/health", headers=sam)[1])
        self.assertEqual((info["user"], info["projects"], info["team"], info["assistant"]), ("sam", 1, True, False))
        self.assertEqual(json.loads(self.call(base + "/api/health", headers=owner)[1])["projects"], 2)
        sam_bundle = self.call(base + "/data/bundle.js", headers=sam)[1].decode("utf-8")
        self.assertIn('"id":"tide-loader:00"', sam_bundle)
        self.assertNotIn('"id":"harbor-notes:', sam_bundle)
        self.assertIn('"id":"harbor-notes:00"', self.call(base + "/data/bundle.js", headers=owner)[1].decode("utf-8"))
        # histories are separate files
        self.assertEqual(self.call(base + "/api/progress", {"updated": 5, "cards": {"tide-loader:00": {"last": 5, "reps": 1}}}, headers=sam)[0], 200)
        self.assertEqual(json.loads(self.call(base + "/api/progress", headers=owner)[1])["cards"], {})
        self.assertEqual(sorted(os.listdir(folder)), ["sam.json"])
        self.assertEqual(self.call(base + "/api/ask", {"question": "x"}, headers=sam, method="POST")[0], 503)   # no credential, no assistant
        self.assertEqual(json.loads(self.call(base + "/openapi.json", headers=sam)[1])["openapi"], "3.1.0")
        self.assertIn(b"POST /api/ask", self.call(base + "/docs", headers=sam)[1])

    def test_users_file_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "users.json")
            for bad in ({"users": []}, {"users": [{"name": "Sam", "token": "t" * 24}]}, {"users": [{"name": "sam", "token": "short"}]},
                        {"users": [{"name": "a", "token": "t" * 24}, {"name": "b", "token": "t" * 24}]}):
                _dump(bad, path)
                with self.assertRaises(ValueError):
                    server.load_users(path)
            _dump({"users": self.USERS}, path)
            self.assertEqual(len(server.load_users(path)), 2)
        with self.assertRaises(ValueError):                                    # a typo in a project list stops the server
            server.Site(demo_edition(), users=[{"name": "x", "token": "t" * 24, "projects": ["tide"]}])

    def test_a_proxy_header_is_believed_only_from_the_named_proxy(self):
        base, _ = self.team()
        self.assertEqual(self.call(base + "/api/health", headers={"X-Forwarded-User": "sam"})[0], 401)
        elsewhere = {"access": {"trust_user_header": "X-Forwarded-User", "trusted_proxies": ["10.42.0.0/16"]}}
        base, _ = self.team(user_header="X-Forwarded-User", settings=elsewhere)             # this test does not come from 10.42.x.x
        self.assertEqual(self.call(base + "/api/health", headers={"X-Forwarded-User": "sam"})[0], 401)
        owner = self.cookie(base, self.USERS[0])
        info = json.loads(self.call(base + "/api/health", headers={**owner, "X-Forwarded-User": "sam"})[1])
        self.assertEqual(info["user"], "owner")                                              # a forged header does not replace a sign-in
        here = {"access": {"trust_user_header": "X-Forwarded-User", "trusted_proxies": ["127.0.0.1"]}}
        base, _ = self.team(user_header="X-Forwarded-User", settings=here)
        self.assertEqual(json.loads(self.call(base + "/api/health", headers={"X-Forwarded-User": "sam"})[1])["user"], "sam")
        self.assertEqual(self.call(base + "/api/health", headers={"X-Forwarded-User": "nobody"})[0], 401)

    def test_no_spelling_of_the_bundle_path_reaches_a_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = os.path.join(tmp, "app")
            os.makedirs(os.path.join(app, "data"))
            os.makedirs(os.path.join(app, "js"))
            with open(os.path.join(app, "data", "bundle.js"), "w") as fh:
                fh.write("window.RECURSER_BUNDLE = 'EVERY-DECK-ON-DISK';")                   # what `recurser build` leaves behind
            with open(os.path.join(app, "index.html"), "w") as fh:
                fh.write("<title>Recurser</title>")
            site = server.Site(demo_edition(), users=[dict(u) for u in self.USERS], progress_dir=tmp)
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, site))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self.addCleanup(httpd.server_close)
            self.addCleanup(httpd.shutdown)
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            sam = self.cookie(base, self.USERS[1])
            proper = self.call(base + "/data/bundle.js", headers=sam)[1].decode("utf-8")
            self.assertTrue(proper.startswith('window.RECURSER_VIEWER = {"user": "sam", "team": true};'))
            for alias in ("/data//bundle.js", "/./data/bundle.js", "/js/../data/bundle.js", "/data/./bundle.js", "/data/bundle.js/",
                          "/data/%62undle.js", "//data/bundle.js", "/data%2Fbundle.js", "/data/"):
                status, body, _ = self.call(base + alias, headers=sam)
                self.assertNotIn(b"EVERY-DECK-ON-DISK", body, alias)
                self.assertNotIn(b"harbor-notes:", body, alias)
                self.assertIn(status, (200, 404), alias)
            # A disk that ignores letter case answers "Data" with the data folder.  Two guards, each tried alone.
            # The name itself: a folder spelled Data is never served, even where it is a folder of its own.
            os.makedirs(os.path.join(app, "Data"))
            with open(os.path.join(app, "Data", "bundle.js"), "w") as fh:
                fh.write("window.RECURSER_BUNDLE = 'EVERY-DECK-ON-DISK';")
            for alias in ("/Data/bundle.js", "/DATA/bundle.js", "/dAtA/bundle.js", "/%44ata/bundle.js", "/\uff24ata/bundle.js"):
                status, body, _ = self.call(base + urllib.parse.quote(alias, safe="/%"), headers=sam)
                self.assertEqual((status, b"EVERY-DECK-ON-DISK" in body), (404, False), alias)
            # The folder itself: a second name for it that path resolution leaves alone, as such a disk does.
            from unittest import mock

            os.symlink(os.path.join(app, "data"), os.path.join(app, "other-name"))
            self.assertEqual(self.call(base + "/other-name/bundle.js", headers=sam)[0], 404)               # resolved: plainly inside data
            with mock.patch.object(server.os.path, "realpath", os.path.abspath):
                status, body, _ = self.call(base + "/other-name/bundle.js", headers=sam)                  # not resolved: the file system is asked
            self.assertEqual((status, b"EVERY-DECK-ON-DISK" in body), (404, False))
            self.assertEqual(self.call(base + "/index.html", headers=sam)[0], 200)                         # everything else is still served

    def test_a_history_holds_only_what_its_person_may_see(self):
        base, _ = self.team()
        sam, owner = self.cookie(base, self.USERS[1]), self.cookie(base, self.USERS[0])
        mixed = {"updated": 9, "cards": {"tide-loader:00": {"last": 9, "reps": 1}, "harbor-notes:00": {"last": 8, "reps": 3}},
                 "log": [{"id": "tide-loader:00", "t": 9, "r": 2}, {"id": "harbor-notes:00", "t": 8, "r": 2}],
                 "flags": {"harbor-notes:60": {"v": "suspended", "t": 5}}, "gaps": {"harbor-notes:00": {"t": 5, "text": "a note that is not sam's"}},
                 "touch": {"harbor-notes": 8, "tide-loader": 9}}
        merged = json.loads(self.call(base + "/api/progress", mixed, headers=sam)[1])       # e.g. a browser that held the owner's history
        stored = json.loads(self.call(base + "/api/progress", headers=sam)[1])
        for history in (merged, stored):
            self.assertEqual((sorted(history["cards"]), [e["id"] for e in history["log"]]), (["tide-loader:00"], ["tide-loader:00"]))
            self.assertEqual((history["flags"], history["gaps"], history["touch"]), ({}, {}, {"tide-loader": 9}))
        self.assertEqual(sorted(json.loads(self.call(base + "/api/progress", mixed, headers=owner)[1])["cards"]), ["harbor-notes:00", "tide-loader:00"])

    def test_odd_bodies_get_an_answer_not_a_dropped_connection(self):
        base, _ = self.team()
        sam = self.cookie(base, self.USERS[1])
        huge = '{"cards": {"tide-loader:00": {"last": ' + "9" * 400 + '}}, "updated": ' + "9" * 400 + "}"
        req = urllib.request.Request(base + "/api/progress", data=huge.encode(), method="PUT", headers=sam)
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual((resp.status, json.loads(resp.read())["updated"]), (200, 0))
        deep = ("[" * 30000 + "]" * 30000).encode()
        req = urllib.request.Request(base + "/api/progress", data=deep, method="PUT", headers=sam)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.call(base + "/api/health", headers=sam)[0], 200)               # the server is still there

    @unittest.skipIf(anthropic is None, "the anthropic package is not installed")
    def test_the_limit_on_answers_counts_the_ones_nobody_waits_for(self):
        import time

        from fake_anthropic import FakeAnthropic

        api = FakeAnthropic()
        self.addCleanup(api.close)
        client = anthropic.Anthropic(api_key="test-key", base_url=api.url, max_retries=0)
        self.addCleanup(client.close)
        base, _ = self.team(client=client, settings={"server": {"max_concurrent_answers": 1}})
        sam = self.cookie(base, self.USERS[1])
        site = self.site
        site.settings["llm"]["answer_seconds"] = 0.4     # shorter than the settings file accepts, for the test
        begun = time.monotonic()
        status, body, _ = self.call(base + "/api/critique", {"id": "tide-loader:00", "answer": "slow one [[id=A; ping:15:0.1; end]]"}, headers=sam, method="POST")
        self.assertEqual((status, json.loads(body)["error"]), (504, "the answer took too long and was abandoned"))
        self.assertLess(time.monotonic() - begun, 1.2)
        self.assertFalse(site.slots.acquire(blocking=False), "the place of an answer the API is still writing was given away")
        # the next person waits for the place instead of adding a second answer in flight
        site.settings["llm"]["answer_seconds"] = 20
        status, body, _ = self.call(base + "/api/critique", {"id": "tide-loader:00", "answer": "quick one [[id=B; text:2:0.01; end]]"}, headers=sam, method="POST")
        self.assertEqual((status, json.loads(body)["text"]), (200, "B-0 B-1"))
        self.assertGreater(time.monotonic() - begun, 1.5)                  # it began when the first stream had ended
        for thread in threading.enumerate():
            if thread.name == "recurser-answer":
                thread.join(10)
        self.assertTrue(site.slots.acquire(blocking=False))                # and the place is free again
        site.slots.release()

    @unittest.skipIf(anthropic is None, "the anthropic package is not installed")
    def test_questions_stay_inside_what_the_person_may_see(self):
        from fake_anthropic import FakeAnthropic

        api = FakeAnthropic()
        self.addCleanup(api.close)
        client = anthropic.Anthropic(api_key="test-key", base_url=api.url, max_retries=0)
        self.addCleanup(client.close)
        base, _ = self.team(client=client, settings={"server": {"ask_per_10_minutes": 3}})
        sam = self.cookie(base, self.USERS[1])
        status, body, _ = self.call(base + "/api/ask", {"question": "Where does harbor-notes keep one Markdown file per note?"}, headers=sam, method="POST")
        answer = json.loads(body)
        self.assertEqual(status, 200)
        sources = [b["source"] for b in api.requests[-1]["body"]["messages"][0]["content"] if b["type"] == "search_result"]
        self.assertTrue(sources and all("harbor-notes" not in src for src in sources))   # sam cannot reach the other deck, even by asking
        self.assertEqual(answer["text"], "Grounded sentence. A closing remark.")
        self.assertEqual(answer["sources"][0]["slug"], "tide-loader")
        self.assertTrue(answer["sources"][0]["text"])
        self.assertEqual(self.call(base + "/api/ask", {"question": "q", "slug": "harbor-notes"}, headers=sam, method="POST")[0], 404)
        self.assertEqual(self.call(base + "/api/ask", {"question": "q", "slug": []}, headers=sam, method="POST")[0], 404)
        self.assertEqual(self.call(base + "/api/ask", {"question": "lone \ud800 surrogate"}, headers=sam, method="POST")[0], 400)   # refused before it counts
        self.assertEqual(self.call(base + "/api/ask", {"question": "x" * 2001}, headers=sam, method="POST")[0], 400)
        self.assertEqual(self.call(base + "/api/critique", {"id": "harbor-notes:00", "answer": "a"}, headers=sam, method="POST")[0], 404)
        status, body, _ = self.call(base + "/api/critique", {"id": "tide-loader:00", "answer": "it loads tide files"}, headers=sam, method="POST")
        self.assertEqual((status, json.loads(body)["text"][:7]), (200, "partly."))
        status, body, headers = self.call(base + "/api/ask", {"question": "one more"}, headers=sam, method="POST")
        self.assertEqual(status, 200)                                           # third allowed question
        status, body, headers = self.call(base + "/api/ask", {"question": "and another"}, headers=sam, method="POST")
        self.assertEqual(status, 429)
        self.assertTrue(int(headers["Retry-After"]) > 0)
        api.mode = "status:401"
        owner = self.cookie(base, self.USERS[0])
        self.assertEqual(self.call(base + "/api/ask", {"question": "what does tide-loader do"}, headers=owner, method="POST")[0], 503)


class FakeCluster:
    """The calls the controller makes, on a dictionary."""

    def __init__(self, objects=()):
        self.objects = {(o["apiVersion"], o["kind"], o["metadata"]["namespace"], o["metadata"]["name"]): o for o in objects}
        self.applied, self.statuses, self.deleted = [], [], []

    def get(self, api_version, kind, namespace, name):
        return self.objects.get((api_version, kind, namespace, name))

    def apply(self, obj):
        self.applied.append(obj)
        key = (obj["apiVersion"], obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"])
        kept = {"status": self.objects[key]["status"]} if "status" in self.objects.get(key, {}) else {}   # an apply never touches the status
        self.objects[key] = {**obj, **kept}

    def delete(self, api_version, kind, namespace, name):
        self.deleted.append((api_version, kind, namespace, name))
        self.objects.pop((api_version, kind, namespace, name), None)

    def set_status(self, app, status):
        self.statuses.append(status)
        app["status"] = status

    def list_apps(self, namespace):
        return [o for (_, kind, ns, _), o in self.objects.items() if kind == k8s.KIND and ns == namespace]

    def watch_apps(self, namespace, timeout):
        return iter(())


class KubernetesTests(unittest.TestCase):
    OWNER = {"apiVersion": k8s.API_VERSION, "kind": k8s.KIND, "name": "team", "uid": "uid-1", "controller": True, "blockOwnerDeletion": True}

    def by_kind(self, spec, **kw):
        return {o["kind"]: o for o in k8s.render("team", "recurser", spec, **kw)}

    def test_the_five_layers(self):
        spec = k8s.example()["app"]["spec"]
        self.assertEqual(k8s.validate_spec(spec), [])
        objs = self.by_kind(spec, owner=self.OWNER, checksums={"config": "7"})
        self.assertEqual(sorted(objs), ["Deployment", "NetworkPolicy", "PersistentVolumeClaim", "Service"])
        pod = objs["Deployment"]["spec"]["template"]["spec"]
        box = pod["containers"][0]
        # 1. the container: an API service on port 8000
        self.assertEqual(box["ports"], [{"name": "http", "containerPort": 8000, "protocol": "TCP"}])
        # what the probes, the Service and the volume rely on is passed as flags, which win over config.yaml
        self.assertEqual(box["args"], ["serve", "--config", "/app/config/config.yaml", "--host", "0.0.0.0", "--port", "8000",
                                       "--progress-dir", "/var/lib/recurser", "--users", "/app/secrets/users.json"])
        self.assertIn({"name": "data", "mountPath": "/var/lib/recurser"}, box["volumeMounts"])
        self.assertEqual(pod["terminationGracePeriodSeconds"], 10)
        # 2. the ConfigMap, mounted at /app/config/config.yaml
        self.assertIn({"name": "config", "mountPath": "/app/config", "readOnly": True}, box["volumeMounts"])
        self.assertEqual(pod["volumes"][0], {"name": "config", "configMap": {"name": "team-config", "items": [{"key": "config.yaml", "path": "config.yaml"}]}})
        # 3. the credential: a reference to a Secret, never a value
        self.assertEqual(box["env"], [{"name": "ANTHROPIC_API_KEY", "valueFrom": {"secretKeyRef": {"name": "team-anthropic", "key": "ANTHROPIC_API_KEY"}}}])
        self.assertNotIn("sk-", json.dumps(objs))
        # 4. the Service: 80 inside the cluster, the container's port behind it
        self.assertEqual(objs["Service"]["spec"]["ports"], [{"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}])
        self.assertEqual(objs["Service"]["spec"]["selector"], objs["Deployment"]["spec"]["selector"]["matchLabels"])
        self.assertTrue(set(objs["Service"]["spec"]["selector"].items()) <= set(objs["Deployment"]["spec"]["template"]["metadata"]["labels"].items()))
        # every generated object belongs to the RecurserApp and goes with it, except the claim that holds the histories
        self.assertTrue(all(o["metadata"]["ownerReferences"] == [self.OWNER] for kind, o in objs.items() if kind != "PersistentVolumeClaim"))
        self.assertNotIn("ownerReferences", objs["PersistentVolumeClaim"]["metadata"])
        # a change of settings rolls the pod
        other = self.by_kind(spec, owner=self.OWNER, checksums={"config": "8"})
        self.assertNotEqual(objs["Deployment"]["spec"]["template"]["metadata"]["annotations"], other["Deployment"]["spec"]["template"]["metadata"]["annotations"])
        # hardening and the network
        self.assertEqual(box["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True, "capabilities": {"drop": ["ALL"]}})
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(objs["Deployment"]["spec"]["replicas"], 1)
        policy = objs["NetworkPolicy"]["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress", "Egress"])
        self.assertEqual(policy["ingress"][0]["ports"], [{"protocol": "TCP", "port": 8000}])
        https = policy["egress"][1]
        self.assertEqual(https["ports"], [{"protocol": "TCP", "port": 443}])
        self.assertTrue({"10.0.0.0/8", "169.254.0.0/16"} <= set(https["to"][0]["ipBlock"]["except"]))

    def test_optional_parts_and_bad_specs(self):
        bare = self.by_kind({"image": "x:1", "configRef": {"name": "c"}})
        box = bare["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(box["env"], [])                                                # no Secret: no key, no users file
        self.assertEqual(box["args"], ["serve", "--config", "/app/config/config.yaml", "--host", "0.0.0.0", "--port", "8000", "--progress-dir", "/var/lib/recurser"])
        some = self.by_kind({"image": "x:1", "configRef": {"name": "c"}, "projects": ["tide-loader", "60"]})
        self.assertEqual(some["Deployment"]["spec"]["template"]["spec"]["containers"][0]["args"][-2:], ["--only", "tide-loader,60"])
        self.assertTrue(k8s.validate_spec({"image": "x:1", "configRef": {"name": "c"}, "projects": "tide-loader"}))
        full = self.by_kind({"image": "x:1", "configRef": {"name": "c"}, "storage": {"existingClaim": "kept"}, "networkPolicy": {"enabled": False},
                             "ingress": {"host": "recurser.example.internal", "className": "nginx", "tlsSecretName": "tls"}})
        self.assertEqual(sorted(full), ["Deployment", "Ingress", "Service"])
        self.assertEqual(full["Deployment"]["spec"]["template"]["spec"]["volumes"][1]["persistentVolumeClaim"], {"claimName": "kept"})
        self.assertEqual(full["Ingress"]["spec"]["tls"], [{"hosts": ["recurser.example.internal"], "secretName": "tls"}])
        with_ingress = self.by_kind({"image": "x:1", "configRef": {"name": "c"}, "ingress": {"host": "h.example.internal"}})
        sources = with_ingress["NetworkPolicy"]["spec"]["ingress"][0]["from"]
        self.assertIn({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}}}, sources)
        users = {"usersRef": {"name": "u"}}
        self.assertEqual(k8s.validate_spec({"image": "x", "configRef": {"name": "c"}, **users}), [])
        self.assertEqual(k8s.validate_spec({"image": "x", "configRef": {"name": "c"}}), ["spec.usersRef: required"])      # never one shared link
        for bad in ({"image": "", "configRef": {"name": "c"}, **users}, {"image": "x", **users}, {"image": "x", "configRef": {"name": "c"}, "nope": 1, **users},
                    {"image": "x", "configRef": {"name": "c"}, "storage": {"size": "lots"}, **users},
                    {"image": "x", "configRef": {"name": "c"}, "ingress": {"tlsSecretName": "t"}, **users}, "not a mapping"):
            self.assertTrue(k8s.validate_spec(bad), bad)

    def test_the_files_on_disk_are_what_the_generator_writes(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML is not installed")
        from recurser import cli

        for rel, text in cli.k8s_files().items():
            self.assertEqual(_text(os.path.join(ROOT, "deploy", "k8s", rel)), text, rel + " is stale: run python3 -m recurser k8s write")
        crd = k8s.crd()
        self.assertEqual(crd["metadata"]["name"], f"{k8s.PLURAL}.{k8s.GROUP}")
        self.assertEqual(crd["spec"]["versions"][0]["subresources"], {"status": {}})

    def app(self, spec=None, **status):
        app = {"apiVersion": k8s.API_VERSION, "kind": k8s.KIND, "metadata": {"name": "team", "namespace": "recurser", "uid": "uid-1", "generation": 1},
               "spec": spec if spec is not None else k8s.example()["app"]["spec"]}
        if status:
            app["status"] = status
        return app

    def cluster(self, config_text="llm:\n  effort: low\n", secret_keys=("ANTHROPIC_API_KEY",), users_keys=("users.json",), skip=()):
        objects = [
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"namespace": "recurser", "name": "team-config", "resourceVersion": "11"}, "data": {"config.yaml": config_text}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"namespace": "recurser", "name": "team-anthropic", "resourceVersion": "12"}, "data": {k: "eA==" for k in secret_keys}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"namespace": "recurser", "name": "team-users", "resourceVersion": "13"}, "data": {k: "eA==" for k in users_keys}},
        ]
        return FakeCluster([o for o in objects if o["metadata"]["name"] not in skip])

    def test_the_controller_generates_the_deployment_and_the_service(self):
        cluster, app = self.cluster(), self.app()
        status = operator.reconcile(cluster, app)
        self.assertEqual([o["kind"] for o in cluster.applied], ["PersistentVolumeClaim", "Deployment", "Service", "NetworkPolicy"])
        self.assertTrue(all(o["metadata"]["ownerReferences"][0]["uid"] == "uid-1" for o in cluster.applied[1:]))
        self.assertNotIn("ownerReferences", cluster.applied[0]["metadata"])                  # the histories outlive the object
        cond = status["conditions"][0]
        self.assertEqual((cond["status"], cond["reason"], status["observedGeneration"], status["url"]), ("False", "Progressing", 1, "http://team.recurser.svc"))
        # once the pod is ready, the status says so; an unchanged state is not written again
        cluster.objects[("apps/v1", "Deployment", "recurser", "team")]["status"] = {"availableReplicas": 1}
        self.assertEqual(operator.reconcile(cluster, app)["conditions"][0]["reason"], "Available")
        writes = len(cluster.statuses)
        operator.reconcile(cluster, app)
        self.assertEqual(len(cluster.statuses), writes)
        # editing the settings rolls the pod: the template annotation follows the ConfigMap's version
        before = cluster.objects[("apps/v1", "Deployment", "recurser", "team")]["spec"]["template"]["metadata"]["annotations"]
        cluster.objects[("v1", "ConfigMap", "recurser", "team-config")]["metadata"]["resourceVersion"] = "99"
        operator.reconcile(cluster, app)
        self.assertNotEqual(cluster.applied[-3]["spec"]["template"]["metadata"]["annotations"], before)

    def test_the_controller_says_what_is_missing_and_creates_nothing(self):
        cases = [
            (self.cluster(skip=("team-config",)), None, "ConfigMissing"),
            (self.cluster(config_text="llm:\n  modle: x\n"), None, "ConfigInvalid"),
            (self.cluster(config_text="llm: [unclosed"), None, "ConfigInvalid"),
            (self.cluster(skip=("team-anthropic",)), None, "SecretMissing"),
            (self.cluster(secret_keys=("OTHER",)), None, "SecretKeyMissing"),
            (self.cluster(skip=("team-users",)), None, "UsersMissing"),
            (self.cluster(users_keys=()), None, "UsersKeyMissing"),
            (self.cluster(), {"image": "", "configRef": {"name": "team-config"}}, "InvalidSpec"),
        ]
        for cluster, spec, reason in cases:
            status = operator.reconcile(cluster, self.app(spec))
            self.assertEqual((status["conditions"][0]["status"], status["conditions"][0]["reason"]), ("False", reason))
            self.assertEqual(cluster.applied, [], reason)
            self.assertEqual(len(cluster.statuses), 1, reason)
        # a ConfigMap whose key holds no data at all
        empty = self.cluster()
        empty.objects[("v1", "ConfigMap", "recurser", "team-config")]["data"] = {}
        self.assertEqual(operator.reconcile(empty, self.app())["conditions"][0]["reason"], "ConfigKeyMissing")

    def test_a_refused_object_is_reported_in_the_status(self):
        cluster, app = self.cluster(), self.app()

        def refuse(obj):
            if obj["kind"] == "PersistentVolumeClaim":
                raise RuntimeError("persistentvolumeclaims \"team-data\" is invalid: spec.resources.requests.storage: Forbidden: field can not be less than previous value")
            FakeCluster.apply(cluster, obj)

        cluster.apply = refuse
        cond = operator.reconcile(cluster, app)["conditions"][0]
        self.assertEqual((cond["status"], cond["reason"]), ("False", "ApplyFailed"))
        self.assertIn("PersistentVolumeClaim team-data was refused", cond["message"])
        # the refusal does not hold the other objects back
        self.assertEqual([o["kind"] for o in cluster.applied], ["Deployment", "Service", "NetworkPolicy"])

    INGRESS = ("networking.k8s.io/v1", "Ingress", "recurser", "team")
    POLICY = ("networking.k8s.io/v1", "NetworkPolicy", "recurser", "team")

    def test_what_the_spec_no_longer_asks_for_is_removed(self):
        base = k8s.example()["app"]["spec"]
        cluster, app = self.cluster(), self.app({**base, "ingress": {"host": "recurser.example.internal"}})
        operator.reconcile(cluster, app)
        self.assertIn(self.INGRESS, cluster.objects)
        self.assertIn(self.POLICY, cluster.objects)
        self.assertEqual(cluster.deleted, [])
        # the Ingress and the NetworkPolicy leave the spec: the controller removes the ones it made, and only those
        app["spec"] = {**base, "networkPolicy": {"enabled": False}}
        status = operator.reconcile(cluster, app)
        self.assertEqual(sorted(cluster.deleted), [self.INGRESS, self.POLICY])
        self.assertEqual(status["conditions"][0]["reason"], "Progressing")
        kept = {("v1", "PersistentVolumeClaim", "recurser", "team-data"), ("apps/v1", "Deployment", "recurser", "team"), ("v1", "Service", "recurser", "team")}
        self.assertTrue(kept <= set(cluster.objects))
        # a second pass finds nothing left to remove
        operator.reconcile(cluster, app)
        self.assertEqual(len(cluster.deleted), 2)

    def test_objects_somebody_else_made_are_left_alone(self):
        cluster = self.cluster()
        app = self.app({**k8s.example()["app"]["spec"], "networkPolicy": {"enabled": False}})
        other = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"namespace": "recurser", "name": "team", "ownerReferences": [{"uid": "someone-else"}]}}
        handmade = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"namespace": "recurser", "name": "team"}}
        cluster.objects[self.INGRESS], cluster.objects[self.POLICY] = other, handmade
        operator.reconcile(cluster, app)
        self.assertEqual(cluster.deleted, [])
        self.assertIs(cluster.objects[self.INGRESS], other)
        self.assertIs(cluster.objects[self.POLICY], handmade)

    def test_a_refused_removal_is_reported_in_the_status(self):
        base = k8s.example()["app"]["spec"]
        cluster, app = self.cluster(), self.app({**base, "ingress": {"host": "recurser.example.internal"}})
        operator.reconcile(cluster, app)

        def refuse(*key):
            raise RuntimeError("ingresses.networking.k8s.io \"team\" is forbidden")

        cluster.delete = refuse
        app["spec"] = base
        cond = operator.reconcile(cluster, app)["conditions"][0]
        self.assertEqual(cond["reason"], "ApplyFailed")
        self.assertIn("Ingress team could not be removed", cond["message"])
        self.assertIn(self.INGRESS, cluster.objects)

    def test_a_name_that_cannot_name_a_service_is_refused(self):
        for bad in ("Team", "9lives", "team_a", "-team", "team-", "t" * 59, "te.am", ""):
            cluster, app = self.cluster(), self.app()
            app["metadata"]["name"] = bad
            cond = operator.reconcile(cluster, app)["conditions"][0]
            self.assertEqual(cond["reason"], "InvalidSpec", bad)
            self.assertIn("metadata.name", cond["message"])
            self.assertEqual(cluster.applied, [], bad)
        for good in ("team", "t", "team-2", "t" * 58):
            self.assertEqual(k8s.validate_name(good), [], good)
        # the longest accepted name still leaves room for the volume claim's suffix
        longest = k8s.render("t" * 58, "recurser", k8s.example()["app"]["spec"])
        self.assertTrue(all(len(o["metadata"]["name"]) <= 63 for o in longest))

    def test_an_image_from_a_private_registry(self):
        controller = [o for o in k8s.controller_manifests("recurser", "registry.example.internal/recurser:1", "regcred") if o["kind"] == "Deployment"][0]
        self.assertEqual(controller["spec"]["template"]["spec"]["imagePullSecrets"], [{"name": "regcred"}])
        declared = k8s.example(image="registry.example.internal/recurser:1", pull_secret="regcred")["app"]["spec"]
        self.assertEqual(declared["imagePullSecrets"], [{"name": "regcred"}])
        self.assertEqual(k8s.validate_spec(declared), [])
        generated = self.by_kind(declared)["Deployment"]["spec"]["template"]["spec"]
        self.assertEqual(generated["imagePullSecrets"], [{"name": "regcred"}])
        # not asked for: not there
        plain = [o for o in k8s.controller_manifests("recurser", "x:1") if o["kind"] == "Deployment"][0]
        self.assertNotIn("imagePullSecrets", plain["spec"]["template"]["spec"])
        self.assertNotIn("imagePullSecrets", k8s.example()["app"]["spec"])

    def test_the_controller_may_do_what_it_does_and_no_more(self):
        role = [o for o in k8s.controller_manifests("recurser", "x:1") if o["kind"] == "Role"][0]
        verbs = {res: set(rule["verbs"]) for rule in role["rules"] for res in rule["resources"]}
        self.assertEqual(verbs["networkpolicies"], {"get", "create", "patch", "delete"})
        self.assertEqual(verbs["ingresses"], {"get", "create", "patch", "delete"})
        for kept in ("services", "persistentvolumeclaims", "deployments"):       # never removed by the controller
            self.assertEqual(verbs[kept], {"get", "create", "patch"}, kept)
        self.assertEqual(verbs["secrets"], {"get"})
        self.assertEqual(verbs["configmaps"], {"get"})

    def test_the_controller_leaves_a_sign_of_life_and_the_probe_reads_it(self):
        from unittest import mock

        controller = [o for o in k8s.controller_manifests("recurser", "x:1") if o["kind"] == "Deployment"][0]
        box = controller["spec"]["template"]["spec"]["containers"][0]
        probe = box["livenessProbe"]["exec"]["command"]
        self.assertEqual(probe[:2], ["python", "-c"])
        self.assertIn(k8s.ALIVE_FILE, probe[2])
        self.assertTrue(k8s.ALIVE_FILE.startswith(box["volumeMounts"][0]["mountPath"] + "/"))       # the only writable place in that pod
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "alive")
            code = probe[2].replace(k8s.ALIVE_FILE, path)
            check = lambda: subprocess.run([sys.executable, "-c", code], capture_output=True).returncode
            self.assertNotEqual(check(), 0)                                # no pass yet
            cluster, stop, lines = self.cluster(), threading.Event(), []
            cluster.objects[(k8s.API_VERSION, k8s.KIND, "recurser", "team")] = self.app()

            def log(line):
                lines.append(line)
                stop.set()

            with mock.patch.dict(os.environ, {"RECURSER_ALIVE_FILE": path}):
                operator.run(cluster, "recurser", resync=1, stop=stop, log=log)
            self.assertEqual(check(), 0)                                   # a pass was made a moment ago
            recent = os.path.getmtime(path) - 300
            os.utime(path, (recent, recent))
            self.assertEqual(check(), 0)                                   # five minutes on a slow cluster: still alive
            old = recent - 600
            os.utime(path, (old, old))
            self.assertEqual(check(), 1)                                   # nothing for a quarter of an hour: restart

    def test_a_long_pause_between_passes_is_watched_in_short_stretches(self):
        cluster, stop, asked, signs = self.cluster(), threading.Event(), [], []

        def watch(namespace, timeout):
            asked.append(timeout)
            signs.append(os.path.getmtime(path))
            if len(asked) == 3:
                stop.set()
            return iter(())

        cluster.watch_apps = watch
        with tempfile.TemporaryDirectory() as tmp:
            from unittest import mock

            path = os.path.join(tmp, "alive")
            with mock.patch.dict(os.environ, {"RECURSER_ALIVE_FILE": path}):
                operator.run(cluster, "recurser", resync=600, stop=stop, log=lambda line: None)
        self.assertEqual(asked, [60, 60, 60])                              # never one request for the ten minutes
        self.assertEqual(len(signs), 3)

    def test_a_read_that_fails_is_said_on_the_object(self):
        for kind in ("ConfigMap", "Secret", "Deployment", "Ingress"):
            cluster, app = self.cluster(), self.app()
            operator.reconcile(cluster, app)                               # a first pass that goes well
            writes = len(cluster.statuses)

            def get(api_version, kind_asked, namespace, name, kind=kind):
                if kind_asked == kind:
                    raise RuntimeError(f"{kind.lower()}s is forbidden: the controller may not read it")
                return FakeCluster.get(cluster, api_version, kind_asked, namespace, name)

            cluster.get = get
            cond = operator.reconcile(cluster, app)["conditions"][0]
            self.assertEqual((cond["status"], cond["reason"]), ("False", "ReadFailed"), kind)
            self.assertIn(f"{kind} ", cond["message"])
            self.assertIn("could not be read: ", cond["message"])
            self.assertEqual(len(cluster.statuses), writes + 1, kind)      # the old status does not stay standing

    def test_the_reason_of_a_failure_is_one_stable_line(self):
        class Refused(Exception):
            def __init__(self, status, reason, body, headers):
                super().__init__(f"{status}\nReason: {reason}\nHTTP response headers: {headers}\nHTTP response body: {body}")
                self.status, self.reason, self.body, self.headers = status, reason, body, headers

        told = json.dumps({"kind": "Status", "message": 'deployments.apps "team" is forbidden:\n  not allowed'})
        self.assertEqual(operator._why(Refused(403, "Forbidden", told, {"Audit-Id": "1"})), 'deployments.apps "team" is forbidden: not allowed')
        self.assertEqual(operator._why(Refused(403, "Forbidden", told.encode(), {"Audit-Id": "2"})), 'deployments.apps "team" is forbidden: not allowed')
        plain = [operator._why(Refused(429, "Too Many Requests", "Too many requests, please try again later.\n", {"Audit-Id": str(n), "Date": f"day {n}"})) for n in (1, 2)]
        self.assertEqual(plain, ["HTTP 429 Too Many Requests: Too many requests, please try again later."] * 2)       # no header in it, so no change
        self.assertEqual(operator._why(Refused(502, "Bad Gateway", "", {"Server": "a proxy"})), "HTTP 502 Bad Gateway")
        self.assertEqual(operator._why(RuntimeError("Read timed out.\n(read timeout=30)")), "Read timed out. (read timeout=30)")
        lines = {operator._why(RuntimeError(f"Max retries exceeded (Caused by ConnectTimeoutError(<HTTPConnection(host='h', port=1) at 0x7ffa8c{n}6f10>, 'timed out'))"))
                 for n in (1, 2, 3)}
        self.assertEqual(lines, {"Max retries exceeded (Caused by ConnectTimeoutError(<HTTPConnection(host='h', port=1)>, 'timed out'))"})

    def test_a_secret_named_like_the_settings_does_not_hide_their_edits(self):
        spec = {**k8s.example()["app"]["spec"], "usersRef": {"name": "config"}, "configRef": {"name": "config"}}
        cluster = FakeCluster([
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"namespace": "recurser", "name": "config", "resourceVersion": "11"}, "data": {"config.yaml": "llm:\n  effort: low\n"}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"namespace": "recurser", "name": "team-anthropic", "resourceVersion": "12"}, "data": {"ANTHROPIC_API_KEY": "eA=="}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"namespace": "recurser", "name": "config", "resourceVersion": "13"}, "data": {"users.json": "eA=="}}])
        app = self.app(spec)
        operator.reconcile(cluster, app)
        stamp = lambda: cluster.objects[("apps/v1", "Deployment", "recurser", "team")]["spec"]["template"]["metadata"]["annotations"]
        first = dict(stamp())
        cluster.objects[("v1", "ConfigMap", "recurser", "config")]["metadata"]["resourceVersion"] = "21"
        operator.reconcile(cluster, app)
        second = dict(stamp())
        self.assertNotEqual(first, second)                                  # the settings changed: the pod is rolled
        cluster.objects[("v1", "Secret", "recurser", "config")]["metadata"]["resourceVersion"] = "22"
        operator.reconcile(cluster, app)
        self.assertNotEqual(second, dict(stamp()))                          # the users changed: rolled again

    def test_only_a_changed_spec_starts_a_pass_between_two_full_ones(self):
        seen = {"metadata": {"generation": 3}, "status": {"observedGeneration": 3}}
        self.assertTrue(operator._settled(seen))                           # e.g. the controller's own status write coming back
        self.assertFalse(operator._settled({"metadata": {"generation": 4}, "status": {"observedGeneration": 3}}))
        self.assertFalse(operator._settled({"metadata": {"generation": 1}}))                              # a new object
        self.assertFalse(operator._settled({"metadata": {}, "status": {}}))
        cluster, stop, lines = self.cluster(), threading.Event(), []
        app = self.app()
        cluster.objects[(k8s.API_VERSION, k8s.KIND, "recurser", "team")] = app
        events = [("MODIFIED", app), ("MODIFIED", app), ("ADDED", app)]    # all three carry the status of the first pass

        def watch(namespace, timeout):
            stop.set()
            return iter(events)

        cluster.watch_apps = watch
        operator.run(cluster, "recurser", resync=30, stop=threading.Event() if False else stop, log=lines.append)
        self.assertEqual(len(lines), 1, lines)                             # the full pass, and nothing for the three events

    def test_every_call_to_the_cluster_has_a_time_limit(self):
        if kubernetes is None:          # the calls only import the name of one error class
            import types
            from unittest import mock

            errors = types.ModuleType("kubernetes.dynamic.exceptions")
            errors.NotFoundError = type("NotFoundError", (Exception,), {})
            stand_in = {"kubernetes": types.ModuleType("kubernetes"), "kubernetes.dynamic": types.ModuleType("kubernetes.dynamic"),
                        "kubernetes.dynamic.exceptions": errors}
            patcher = mock.patch.dict(sys.modules, stand_in)
            patcher.start()
            self.addCleanup(patcher.stop)
        calls = []

        class Resource:
            status = None

            def __getattr__(self, verb):
                def call(**kw):
                    calls.append((verb, kw))
                    if verb == "get" and "name" not in kw:
                        return type("Found", (), {"items": []})()
                    return type("One", (), {"to_dict": lambda self: {}})()
                return call

        resource = Resource()
        resource.status = resource
        cluster = operator.KubeCluster.__new__(operator.KubeCluster)        # no cluster needed: the client object is replaced
        cluster._cache = {}
        cluster._resource = lambda api_version, kind: resource
        cluster.get("v1", "ConfigMap", "recurser", "team-config")
        cluster.apply({"apiVersion": "v1", "kind": "Service", "metadata": {"name": "team", "namespace": "recurser"}})
        cluster.delete("networking.k8s.io/v1", "Ingress", "recurser", "team")
        cluster.set_status({"metadata": {"name": "team", "namespace": "recurser"}}, {})
        cluster.list_apps("recurser")
        self.assertEqual([verb for verb, _ in calls], ["get", "server_side_apply", "delete", "patch", "get"])
        for verb, kw in calls:
            self.assertEqual(kw.get("_request_timeout"), operator.KubeCluster.TIMEOUT, verb)

    def test_the_loop_survives_a_bad_object(self):
        cluster = self.cluster()
        good, broken = self.app(), self.app()
        broken["metadata"] = {"name": "broken", "namespace": "recurser"}          # no uid: owner reference cannot be built
        cluster.objects[(k8s.API_VERSION, k8s.KIND, "recurser", "broken")] = broken
        cluster.objects[(k8s.API_VERSION, k8s.KIND, "recurser", "team")] = good
        stop, lines = threading.Event(), []

        def log(line):
            lines.append(line)
            if len(lines) >= 2:
                stop.set()

        operator.run(cluster, "recurser", resync=1, stop=stop, log=log)
        self.assertTrue(any("reconcile failed" in line for line in lines))
        self.assertTrue(any("team: Ready=" in line for line in lines))


@unittest.skipUnless(kubernetes, "needs the official client: pip install kubernetes")
class ControllerThroughTheClientTests(unittest.TestCase):
    """The controller's calls, sent by the official Python client to a local stand-in for the API server."""

    def setUp(self):
        from unittest import mock

        from fake_kube_api import FakeKubeApi
        from kubernetes import client

        self.api = FakeKubeApi((k8s.API_VERSION, k8s.PLURAL, k8s.KIND))
        self.addCleanup(self.api.close)
        tmp = tempfile.TemporaryDirectory()          # the client keeps a discovery cache file in the temp folder
        self.addCleanup(tmp.cleanup)
        self.cache_dir = tmp.name
        patcher = mock.patch.object(tempfile, "tempdir", tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        configuration = client.Configuration(host=self.api.url)
        configuration.proxy = None                   # a local server, whatever the environment says about proxies
        self.cluster = operator.KubeCluster(configuration)
        self.addCleanup(self.cluster.dyn.client.close)
        meta = lambda name: {"namespace": "recurser", "name": name}
        self.api.put({"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("team-config"), "data": {"config.yaml": "llm:\n  effort: low\n"}})
        self.api.put({"apiVersion": "v1", "kind": "Secret", "metadata": meta("team-anthropic"), "data": {"ANTHROPIC_API_KEY": "eA=="}})
        self.api.put({"apiVersion": "v1", "kind": "Secret", "metadata": meta("team-users"), "data": {"users.json": "eA=="}})
        spec = {**k8s.example()["app"]["spec"], "ingress": {"host": "recurser.example.internal"}}
        self.api.put({"apiVersion": k8s.API_VERSION, "kind": k8s.KIND, "metadata": meta("team"), "spec": spec})

    def app(self):
        return self.cluster.list_apps("recurser")[0]

    def test_generate_report_and_remove(self):
        app = self.app()
        self.assertEqual((app["metadata"]["name"], bool(app["metadata"]["uid"])), ("team", True))
        self.assertEqual(operator.reconcile(self.cluster, app)["conditions"][0]["reason"], "Progressing")
        # every generated object went as a server-side apply under the controller's name
        applies = [r for r in self.api.sent("PATCH") if r["content_type"] == "application/apply-patch+yaml"]
        self.assertEqual([r["path"].split("/")[-2:] for r in applies],
                         [["persistentvolumeclaims", "team-data"], ["deployments", "team"], ["services", "team"], ["networkpolicies", "team"], ["ingresses", "team"]])
        for r in applies:
            self.assertEqual(r["query"]["fieldManager"], [k8s.MANAGER], r["path"])
            self.assertEqual(r["query"]["force"][0].lower(), "true", r["path"])
        deployment = self.api.get("apps/v1", "deployments", "recurser", "team")
        self.assertEqual(deployment["metadata"]["ownerReferences"][0]["uid"], app["metadata"]["uid"])
        self.assertNotIn("ownerReferences", self.api.get("v1", "persistentvolumeclaims", "recurser", "team-data")["metadata"])
        self.assertEqual(deployment["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"], 8000)
        # the status went to the status subresource, as a merge patch
        written = self.api.sent("PATCH", "/recurserapps/team/status")
        self.assertEqual((len(written), written[0]["content_type"]), (1, "application/merge-patch+json"))
        stored = self.api.get(k8s.API_VERSION, k8s.PLURAL, "recurser", "team")
        self.assertEqual((stored["status"]["conditions"][0]["reason"], stored["status"]["url"]), ("Progressing", "http://team.recurser.svc"))
        self.assertIn("ingress", stored["spec"])                                # the status patch left the spec alone
        # the pod becomes ready: the next pass says so, and a pass that changes nothing writes nothing
        self.api.put({**deployment, "status": {"availableReplicas": 1}})
        self.assertEqual(operator.reconcile(self.cluster, self.app())["conditions"][0]["reason"], "Available")
        operator.reconcile(self.cluster, self.app())
        self.assertEqual(len(self.api.sent("PATCH", "/recurserapps/team/status")), 2)
        # the Ingress and the NetworkPolicy leave the spec: both are removed, nothing else is
        stored = self.api.get(k8s.API_VERSION, k8s.PLURAL, "recurser", "team")
        self.api.put({**stored, "spec": {**k8s.example()["app"]["spec"], "networkPolicy": {"enabled": False}}})
        operator.reconcile(self.cluster, self.app())
        self.assertEqual(sorted(r["path"].split("/")[-2] for r in self.api.sent("DELETE")), ["ingresses", "networkpolicies"])
        self.assertIsNone(self.api.get("networking.k8s.io/v1", "ingresses", "recurser", "team"))
        self.assertIsNotNone(self.api.get("v1", "persistentvolumeclaims", "recurser", "team-data"))
        # what is not there reads as nothing, and removing it again is not an error
        self.assertIsNone(self.cluster.get("v1", "ConfigMap", "recurser", "no-such-config"))
        self.cluster.delete("networking.k8s.io/v1", "Ingress", "recurser", "team")

    def test_the_words_of_the_api_reach_the_status(self):
        words = 'PersistentVolumeClaim "team-data" is invalid: spec.resources.requests.storage: Forbidden: field can not be less than previous value'
        self.api.refuse[("PATCH", "persistentvolumeclaims")] = (422, "Invalid", words)
        cond = operator.reconcile(self.cluster, self.app())["conditions"][0]
        self.assertEqual(cond["reason"], "ApplyFailed")
        self.assertEqual(cond["message"], "PersistentVolumeClaim team-data was refused: " + words)
        self.assertIsNotNone(self.api.get("apps/v1", "deployments", "recurser", "team"))        # the others were still applied
        self.assertEqual(self.api.get(k8s.API_VERSION, k8s.PLURAL, "recurser", "team")["status"]["conditions"][0]["reason"], "ApplyFailed")
        # a missing ConfigMap: nothing is generated, and the object says what to create
        self.api.refuse.clear()
        del self.api.objects[("v1", "configmaps", "recurser", "team-config")]
        before = len(self.api.sent("PATCH", "/deployments/team"))
        cond = operator.reconcile(self.cluster, self.app())["conditions"][0]
        self.assertEqual((cond["reason"], cond["message"]), ("ConfigMissing", "ConfigMap team-config was not found"))
        self.assertEqual(len(self.api.sent("PATCH", "/deployments/team")), before)

    def test_the_loop_lists_watches_and_follows_a_change(self):
        stop, lines = threading.Event(), []

        def log(line):
            lines.append(line)
            if len(lines) == 1:                      # after the first full pass somebody edits the spec
                stored = self.api.get(k8s.API_VERSION, k8s.PLURAL, "recurser", "team")
                self.api.put({**stored, "spec": {**stored["spec"], "image": "registry.example.internal/recurser:2"}})
            if len(lines) >= 2:                      # the change, seen through the watch
                stop.set()

        worker = threading.Thread(target=operator.run, args=(self.cluster, "recurser"), kwargs={"resync": 20, "stop": stop, "log": log, "pause": 0.1}, daemon=True)
        worker.start()
        worker.join(15)
        self.assertFalse(worker.is_alive(), lines)
        self.assertEqual(lines, ["team: Ready=False (Progressing)"] * 2)
        watch = [r for r in self.api.sent("GET", "/recurserapps") if "watch" in r["query"]]
        self.assertEqual((watch[0]["query"]["watch"][0].lower(), watch[0]["query"]["timeoutSeconds"]), ("true", ["20"]))
        image = self.api.get("apps/v1", "deployments", "recurser", "team")["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, "registry.example.internal/recurser:2")
        stored = self.api.get(k8s.API_VERSION, k8s.PLURAL, "recurser", "team")
        self.assertEqual((stored["metadata"]["generation"], stored["status"]["observedGeneration"]), (2, 2))
        self.assertEqual(len(self.api.sent("PATCH", "/recurserapps/team/status")), 2)       # one per version of the spec

    def test_its_own_status_writes_do_not_start_new_passes(self):
        """A refusal that is not the API's JSON (a proxy's page, an answer that sheds load) carries headers that
        differ every time.  The status says one stable line, and writing it does not bring on another pass."""
        import time

        self.api.plain_refusals = True
        self.api.refuse[("PATCH", "deployments")] = (429, "Too Many Requests", "Too many requests, please try again later.")
        stop, lines = threading.Event(), []
        worker = threading.Thread(target=operator.run, args=(self.cluster, "recurser"), kwargs={"resync": 30, "stop": stop, "log": lines.append, "pause": 0.1}, daemon=True)
        worker.start()
        time.sleep(3)
        stop.set()
        worker.join(40)
        self.assertEqual(lines, ["team: Ready=False (ApplyFailed)"])                         # one pass in three seconds, not hundreds
        self.assertEqual(len(self.api.sent("PATCH", "/recurserapps/team/status")), 1)
        message = self.api.get(k8s.API_VERSION, k8s.PLURAL, "recurser", "team")["status"]["conditions"][0]["message"]
        self.assertEqual(message, "Deployment team was refused: HTTP 429 Too Many Requests: Too many requests, please try again later.")

    def test_a_read_the_cluster_refuses_reaches_the_status(self):
        operator.reconcile(self.cluster, self.app())
        words = 'secrets "team-anthropic" is forbidden: User "system:serviceaccount:recurser:recurser-controller" cannot get resource "secrets"'
        self.api.refuse[("GET", "secrets")] = (403, "Forbidden", words)
        stored = self.api.get(k8s.API_VERSION, k8s.PLURAL, "recurser", "team")
        self.api.put({**stored, "spec": {**stored["spec"], "image": "registry.example.internal/recurser:2"}})
        cond = operator.reconcile(self.cluster, self.app())["conditions"][0]
        self.assertEqual((cond["reason"], cond["message"]), ("ReadFailed", "Secret team-anthropic could not be read: " + words))
        status = self.api.get(k8s.API_VERSION, k8s.PLURAL, "recurser", "team")["status"]
        self.assertEqual((status["conditions"][0]["reason"], status["observedGeneration"]), ("ReadFailed", 2))

    def test_a_request_is_made_once_and_never_waits_without_limit(self):
        """Also the requests the client makes on its own to learn what the cluster offers."""
        import time
        from unittest import mock

        from kubernetes import client

        patcher = mock.patch.object(operator.KubeCluster, "TIMEOUT", (1, 1))      # one second in place of thirty, for the whole test
        patcher.start()
        self.addCleanup(patcher.stop)

        def fresh():
            for name in os.listdir(self.cache_dir):      # no memory of what an earlier client learnt
                os.remove(os.path.join(self.cache_dir, name))
            configuration = client.Configuration(host=self.api.url)
            configuration.proxy = None
            cluster = operator.KubeCluster(configuration)
            self.addCleanup(cluster.dyn.client.close)
            return cluster

        def unanswered(part, call):
            self.api.mute = {part: 6}
            before, begun = len(self.api.requests), time.monotonic()
            with self.assertRaises(Exception):
                call()
            self.assertLess(time.monotonic() - begun, 4, part)                    # one second of patience, not six
            self.api.mute = {}
            return [r["path"] for r in self.api.requests[before:] if part in r["path"]]

        cluster = fresh()
        asked = unanswered("/configmaps/team-config", lambda: cluster.get("v1", "ConfigMap", "recurser", "team-config"))
        self.assertEqual(asked, ["/api/v1/namespaces/recurser/configmaps/team-config"])              # asked once: no second try
        self.assertIsNotNone(cluster.get("v1", "ConfigMap", "recurser", "team-config"))              # the cluster answers again: so does the client
        other = fresh()
        asked = unanswered("/apis/networking.k8s.io/v1", lambda: other.get("networking.k8s.io/v1", "Ingress", "recurser", "team"))
        self.assertEqual(asked, ["/apis/networking.k8s.io/v1"])                   # the document that lists the kinds, asked once

    def test_a_throttled_request_is_waited_out_and_a_refusal_keeps_the_words_of_the_api(self):
        import time

        self.cluster.get("v1", "ConfigMap", "recurser", "team-config")     # learn the kinds first, so that only the read below is counted
        self.api.throttle[("GET", "configmaps")] = 1                       # 429 with Retry-After: 1, once
        begun = time.monotonic()
        self.assertIsNotNone(self.cluster.get("v1", "ConfigMap", "recurser", "team-config"))
        self.assertGreater(time.monotonic() - begun, 0.9)                  # the second it asked for
        self.assertEqual(operator.reconcile(self.cluster, self.app())["conditions"][0]["reason"], "Progressing")
        self.api.throttle[("GET", "configmaps")] = 5                       # throttled again and again: said as the API says it
        cond = operator.reconcile(self.cluster, self.app())["conditions"][0]
        self.assertEqual((cond["reason"], cond["message"]), ("ReadFailed", "ConfigMap team-config could not be read: Too many requests, please try again later."))
        self.api.throttle.clear()
        self.assertEqual(operator.reconcile(self.cluster, self.app())["conditions"][0]["reason"], "Progressing")

    def test_a_connection_that_went_silent_does_not_stop_the_loop(self):
        import time
        from unittest import mock

        self.api.silent_watch = 6
        stop, lines = threading.Event(), []
        with mock.patch.object(operator.KubeCluster, "WATCH_MARGIN", 1):
            worker = threading.Thread(target=operator.run, args=(self.cluster, "recurser"),
                                      kwargs={"resync": 1, "stop": stop, "log": lines.append, "pause": 0.1}, daemon=True)
            begun = time.monotonic()
            worker.start()
            while time.monotonic() - begun < 12 and len(self.api.sent("GET", "/recurserapps")) < 4:      # list, watch, list, watch
                time.sleep(0.05)
            took = time.monotonic() - begun
            stop.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertLess(took, 5, lines)              # given up after two seconds of silence, not the six the server stays mute
        self.assertTrue(any(line.startswith("watch interrupted") for line in lines), lines)
        self.assertGreaterEqual(sum(line.startswith("team: Ready=") for line in lines), 2)                # a second full pass was made


class ContainerProcessTests(unittest.TestCase):
    """The server and the controller as a pod starts them: real processes, the pod's flags, and (where this
    machine allows it) as the first process of a process namespace, which is what a container's command is."""

    FIRST = ["unshare", "--pid", "--fork", "--kill-child", "--mount-proc"]      # --kill-child: nothing outlives a failed test

    @staticmethod
    def free_port():
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @classmethod
    def ways(cls):
        """How to start the process: plainly, and as process 1 of its own namespace when that is permitted here."""
        try:
            allowed = subprocess.run(cls.FIRST + ["true"], capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            allowed = False
        return [False, True] if allowed else [False]

    def start(self, argv, env, first):
        """Start ``argv``; returns the process to wait for and the process id that takes the stop signal."""
        import time

        proc = subprocess.Popen((self.FIRST if first else []) + argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(), proc.stdout.close()))
        if not first:
            return proc, proc.pid
        for _ in range(100):                           # the command is the one child of `unshare`
            try:
                children = _text(f"/proc/{proc.pid}/task/{proc.pid}/children").split()
            except OSError:
                children = []
            if children:
                pid = int(children[0])
                inside = [line for line in _text(f"/proc/{pid}/status").splitlines() if line.startswith("NSpid:")]
                self.assertEqual(inside[0].split()[-1], "1", "the command is not the first process of its namespace")
                return proc, pid
            time.sleep(0.05)
        self.fail("the command did not start")

    def stop(self, proc, pid):
        """Send the stop request a pod receives; the process must end by itself, quickly, with a clean exit."""
        import signal
        import time

        begun = time.monotonic()
        os.kill(pid, signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=8), 0)
        self.assertLess(time.monotonic() - begun, 5)

    @staticmethod
    def environment(**extra):
        """This environment without its proxy settings: everything here talks to servers on this machine."""
        return {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "PYTHONUNBUFFERED": "1", **extra}

    def test_the_pod_flags_win_over_the_settings_and_a_stop_request_ends_the_server(self):
        import time

        demo_edition()
        link = DEMO_LINK + "-owner-0123456789"
        for first in self.ways():
            with self.subTest(first_process=first), tempfile.TemporaryDirectory() as tmp:
                elsewhere, pinned = self.free_port(), self.free_port()
                settings = os.path.join(tmp, "config.yaml")           # JSON is YAML: no extra package needed here
                _dump({"llm": {"enabled": False}, "server": {"host": "127.0.0.1", "port": elsewhere, "progress_dir": os.path.join(tmp, "elsewhere")}}, settings)
                users = os.path.join(tmp, "users.json")
                _dump({"users": [{"name": "owner", "token": link, "projects": "*"}]}, users)
                proc, pid = self.start([sys.executable, "-m", "recurser", "--data-root", DEMO, "serve", "--config", settings, "--host", "127.0.0.1",
                                        "--port", str(pinned), "--progress-dir", os.path.join(tmp, "pinned"), "--users", users], self.environment(), first)
                answered = None
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{pinned}/healthz", timeout=2) as resp:
                            answered = resp.status
                            break
                    except OSError:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.1)
                self.assertEqual(answered, 200, "the server did not come up on the port given as a flag")
                with self.assertRaises(OSError):
                    urllib.request.urlopen(f"http://127.0.0.1:{elsewhere}/healthz", timeout=2)
                # a history lands in the folder given as a flag
                req = urllib.request.Request(f"http://127.0.0.1:{pinned}/api/progress", method="PUT", data=json.dumps({"version": 2, "cards": {}}).encode(),
                                             headers={"Content-Type": "application/json", "Cookie": "recurser_token=" + link})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                self.assertTrue(os.path.exists(os.path.join(tmp, "pinned", "owner.json")))
                self.assertFalse(os.path.exists(os.path.join(tmp, "elsewhere")))
                self.stop(proc, pid)
                self.assertNotIn(DEMO_LINK, proc.stdout.read())        # no sign-in value in what the pod logs

    def test_a_stop_request_ends_other_commands_with_a_failure_status(self):
        """Also when one of their arguments happens to read "serve"."""
        import signal
        import time

        demo_edition()
        slow = "with recursive c(x) as (select 1 union all select x + 1 from c where x < 400000000) select count(*) from c"
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(DEMO, os.path.join(tmp, "serve"))
            for data_root, cwd in ((DEMO, ROOT), ("serve", tmp)):
                env = self.environment(PYTHONPATH=ROOT)
                proc = subprocess.Popen([sys.executable, "-m", "recurser", "--root", ROOT, "--data-root", data_root, "sql", slow], cwd=cwd, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                self.addCleanup(lambda p=proc: (p.poll() is None and p.kill(), p.wait(), p.stdout.close()))
                time.sleep(1.5)
                self.assertIsNone(proc.poll(), "the query ended before the stop request: " + data_root)
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=8), -signal.SIGTERM, data_root)       # ended by the request, at once, not with success

    @unittest.skipUnless(kubernetes, "needs the official client: pip install kubernetes")
    def test_the_controller_process_reconciles_shows_life_and_stops(self):
        import time

        from fake_kube_api import FakeKubeApi

        for first in self.ways():
            with self.subTest(first_process=first), tempfile.TemporaryDirectory() as tmp:
                api = FakeKubeApi((k8s.API_VERSION, k8s.PLURAL, k8s.KIND))
                self.addCleanup(api.close)
                meta = lambda name: {"namespace": "recurser", "name": name}
                api.put({"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("team-config"), "data": {"config.yaml": "llm:\n  effort: low\n"}})
                api.put({"apiVersion": "v1", "kind": "Secret", "metadata": meta("team-anthropic"), "data": {"ANTHROPIC_API_KEY": "eA=="}})
                api.put({"apiVersion": "v1", "kind": "Secret", "metadata": meta("team-users"), "data": {"users.json": "eA=="}})
                api.put({"apiVersion": k8s.API_VERSION, "kind": k8s.KIND, "metadata": meta("team"), "spec": k8s.example()["app"]["spec"]})
                where = os.path.join(tmp, "kubeconfig")                # how the client finds a cluster outside one
                _dump({"apiVersion": "v1", "kind": "Config", "current-context": "here",
                       "clusters": [{"name": "here", "cluster": {"server": api.url}}], "users": [{"name": "controller", "user": {}}],
                       "contexts": [{"name": "here", "context": {"cluster": "here", "user": "controller"}}]}, where)
                alive = os.path.join(tmp, "alive")
                env = self.environment(KUBECONFIG=where, RECURSER_ALIVE_FILE=alive, TMPDIR=tmp)
                proc, pid = self.start([sys.executable, "-m", "recurser.operator", "--namespace", "recurser", "--resync", "2"], env, first)
                for _ in range(150):                                   # a first full pass ends where the watch begins
                    if any("watch" in r["query"] for r in api.sent("GET", "/recurserapps")) or proc.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertIsNone(proc.poll(), "the controller ended by itself")
                self.assertIsNotNone(api.get("apps/v1", "deployments", "recurser", "team"), "no Deployment was generated")
                self.assertIsNotNone(api.get("v1", "services", "recurser", "team"))
                status = api.get(k8s.API_VERSION, k8s.PLURAL, "recurser", "team").get("status") or {"conditions": [{}]}
                self.assertEqual(status["conditions"][0].get("reason"), "Progressing")
                self.assertTrue(os.path.exists(alive))                 # what the liveness probe reads
                self.stop(proc, pid)
                self.assertIn("team: Ready=False (Progressing)", proc.stdout.read())


if __name__ == "__main__":
    unittest.main()
