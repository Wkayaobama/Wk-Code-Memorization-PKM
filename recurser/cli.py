"""Command line for Recurser.

    python -m recurser inventory            last-accessed table for every repository
    python -m recurser match "tide load"    fuzzy-find a project by any name you use for it
    python -m recurser build                rebuild the app data (and optional single-file app)
    python -m recurser serve                open the app on http://127.0.0.1:8765
    python -m recurser xlsx out.xlsx        the fillable matrix workbook
    python -m recurser anki out_dir         Anki exports (csv, txt and .apkg when genanki is installed)
    python -m recurser kb out_dir           one Markdown page per project for an Obsidian / TiddlyWiki vault
    python -m recurser verify --sources-root DIR   re-check every card against fresh checkouts
    python -m recurser sql "select ..."     query the in-memory database
    python -m recurser stats                counts for the current edition
    python -m recurser validate map|cards FILE [--repo DIR]
    python -m recurser ingest ...           file analysis output into the data tiers
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

from . import address, bundle as bundle_mod, db, fuzzy, inventory, schema, store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tiers(args) -> list[str] | None:
    value = getattr(args, "tiers", None)
    tiers = [t.strip() for t in value.split(",") if t.strip()] if value else None
    unknown = [t for t in tiers or [] if t not in store.TIERS]
    if unknown:
        sys.exit(f"Unknown tier {unknown[0]!r}: use {' or '.join(store.TIERS)}, separated by a comma.")
    return tiers


def _today(args) -> date:
    value = getattr(args, "today", None)
    return date.fromisoformat(value) if value else datetime.now(timezone.utc).date()


def _data(args) -> str:
    return getattr(args, "data_root", None) or args.root


def _edition(args) -> dict:
    tiers = _tiers(args) or store.present_tiers(_data(args))
    if not tiers:
        sys.exit("No data tier found under data/. Run `python -m recurser ingest ...` or add data/public.")
    edition = store.load_edition(_data(args), tiers, _today(args))
    for warning in edition.get("warnings") or []:
        print("note:", warning, file=sys.stderr)
    return edition


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "(no rows)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    line = "  ".join(c.ljust(widths[c]) for c in columns)
    out = [line, "  ".join("-" * widths[c] for c in columns)]
    out += ["  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows]
    return "\n".join(out)


# -- commands ---------------------------------------------------------------
def cmd_inventory(args) -> int:
    if args.refresh:
        try:
            repos = inventory.fetch_github(args.owner or "")
            print(f"refreshed {len(repos)} repositories from GitHub", file=sys.stderr)
        except RuntimeError as exc:
            print(f"{exc}\nkeeping the stored snapshot.", file=sys.stderr)
            repos = None
        if repos and args.save:
            _refuse_private_output(args, any(not inventory.is_public(r.get("visibility")) for r in repos), args.save)
            with open(args.save, "w", encoding="utf-8") as fh:
                json.dump({"as_of": _today(args).isoformat(), "owner": args.owner or "", "repos": repos}, fh, indent=1)
            print(f"saved {args.save}", file=sys.stderr)
    else:
        repos = None
    if repos is None:
        if args.snapshot:
            repos = inventory.load_snapshot(args.snapshot)
        else:
            repos = []
            for tier in store.present_tiers(_data(args)) or store.TIERS:
                path = os.path.join(store.tier_dir(_data(args), tier), "inventory.json")
                if os.path.exists(path):
                    repos.extend(inventory.load_snapshot(path))
    analyzed = inventory.analyze(repos, _today(args))
    if args.recent:
        analyzed = inventory.select_recent(analyzed, days=args.days, include_forks=args.forks)
    rows = [{"repository": r["name"], "last push": r["pushed_at"][:10], "days": r["days_since"], "tier": r["tier_label"],
             "unrehearsed": f"{round(r['retention'] * 100)}%", "kind": ("fork " if r["fork"] else "") + r["visibility"]} for r in analyzed]
    print(_table(rows, ["repository", "last push", "days", "tier", "unrehearsed", "kind"]))
    print(f"\n{len(rows)} repositories" + (f" touched in the last {args.days} days" if args.recent else ""))
    return 0


def cmd_match(args) -> int:
    edition = _edition(args)
    items = list(edition["projects"]) + [r for r in edition["inventory"] if not r.get("slug")]
    hits = fuzzy.rank(args.query, items, limit=args.limit, threshold=0.3)
    rows = [{"score": f"{h['score']:.2f}", "project": h["item"].get("title") or h["item"].get("name"),
             "repository": h["item"].get("repo") or h["item"].get("name"), "code": h["item"].get("code", ""),
             "matched on": h["why"][:70]} for h in hits]
    print(_table(rows, ["score", "code", "project", "repository", "matched on"]))
    return 0 if hits else 1


def cmd_validate(args) -> int:
    return schema._main(["schema", args.kind, args.file] + [x for r in args.repo for x in ("--repo", r)])


def cmd_ingest(args) -> int:
    with open(args.codes, encoding="utf-8") as fh:
        codes = json.load(fh)
    aliases = {}
    if args.aliases:
        with open(args.aliases, encoding="utf-8") as fh:
            aliases = json.load(fh)
    scrub = None
    if args.scrub:
        with open(args.scrub, encoding="utf-8") as fh:
            scrub = json.load(fh)
    report = store.ingest(_data(args), manifest_path=args.manifest, maps_dir=args.maps, cards_dir=args.cards, codes=codes,
                          snapshot_path=args.snapshot, aliases=aliases, as_of=args.as_of, owner=args.owner or "", scrub=scrub)
    for item in report["skipped"]:
        print(f"SKIPPED {item['slug']}: {item['why']}", file=sys.stderr)
        for err in item.get("errors", []):
            print(f"         {err}", file=sys.stderr)
    print(f"\nfiled {len(report['filed'])} projects, skipped {len(report['skipped'])}")
    return 0 if report["filed"] else 1


def _git_ignores(root: str, path: str) -> bool | None:
    """True / False when git can tell whether ``path`` is ignored in the checkout at ``root``, else None."""
    import subprocess

    try:
        out = subprocess.run(["git", "-C", root, "check-ignore", "-q", path], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(out.returncode)


def _refuse_private_output(args, private: bool, *paths) -> None:
    """A private edition must not land where it would be published.

    ``docs/`` is the folder a public site is served from: never there.  And when this checkout keeps
    ``data/private/`` out of git (it is the public repository), not in any other tracked place either:
    ``dist/`` and other ignored folders, or anywhere outside the checkout, are fine."""
    if not private:
        return
    root = os.path.realpath(args.root)
    docs = os.path.join(root, "docs") + os.sep
    public_checkout = _git_ignores(root, os.path.join(root, "data", "private", "x.json")) is True
    for path in paths:
        if not path:
            continue
        real = os.path.realpath(path)
        if (real + os.sep).startswith(docs):
            sys.exit(f"Refusing to write a private edition into docs/ ({path}). Build the public tier (--tiers public) or pick another folder.")
        inside = (real + os.sep).startswith(root + os.sep)
        if inside and public_checkout and _git_ignores(root, real) is False:
            sys.exit(f"Refusing to write a private edition to {path}: git would track it in this public checkout. "
                     "Write it under dist/ or outside the repository, or build with --tiers public.")


def _refuse_private_in_docs(args, edition: dict, *paths) -> None:
    _refuse_private_output(args, edition.get("edition") != "public", *paths)


def cmd_build(args) -> int:
    edition = _edition(args)
    _refuse_private_in_docs(args, edition, args.single_file, args.fragment)
    data = bundle_mod.build_bundle(edition)
    path = bundle_mod.write_bundle(args.root, data)
    stats = store.edition_stats(edition)
    print(f"{path}: {stats['cards']} cards, {stats['projects']} projects, {stats['repositories']} repositories ({data['edition']} edition)")
    if args.single_file:
        print("wrote", bundle_mod.write_single_file(args.root, data, args.single_file))
    if args.fragment:
        print("wrote", bundle_mod.write_single_file(args.root, data, args.fragment, fragment=True))
    return 0


def cmd_stats(args) -> int:
    print(json.dumps(store.edition_stats(_edition(args)), indent=1))
    return 0


def cmd_sql(args) -> int:
    edition = _edition(args)
    con = db.build(edition)
    if args.save:
        _refuse_private_in_docs(args, edition, args.save)
        db.dump(con, args.save)
        print(f"saved {args.save}", file=sys.stderr)
    rows = db.query(con, args.query)
    print(_table(rows, list(rows[0].keys())) if rows else "(no rows)")
    return 0


def cmd_xlsx(args) -> int:
    from . import xlsx

    progress = None
    if args.progress:
        with open(args.progress, encoding="utf-8") as fh:
            progress = json.load(fh)
    edition = _edition(args)
    _refuse_private_in_docs(args, edition, args.out)
    path = xlsx.build_workbook(edition, args.out, progress=progress, keep_from=args.keep_from)
    print("wrote", path)
    return 0


def _progress(args):
    if not getattr(args, "progress", None):
        return None
    with open(args.progress, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_anki(args) -> int:
    from . import anki

    if args.only_learned and not args.progress:
        sys.exit("--only-learned needs --progress <history export from the app>")
    edition = _edition(args)
    _refuse_private_in_docs(args, edition, args.out)
    for path in anki.export_all(edition, args.out, apkg=not args.no_apkg, progress=_progress(args), only_learned=args.only_learned):
        print("wrote", path)
    return 0


def cmd_kb(args) -> int:
    from . import kb

    edition = _edition(args)
    _refuse_private_in_docs(args, edition, args.out)
    paths = kb.export_kb(edition, args.out, progress=_progress(args))
    print(f"wrote {len(paths)} pages to {args.out}")
    return 0


def cmd_verify(args) -> int:
    report = store.find_stale(_data(args), args.sources_root, _tiers(args))
    for slug, keys in sorted(report["stale"].items()):
        print(f"STALE     {slug}: {', '.join(keys)}")
    for slug in report["unchecked"]:
        print(f"unchecked {slug}: no checkout under {args.sources_root}")
    stale = sum(len(v) for v in report["stale"].values())
    print(f"\n{report['confirmed']} cards confirmed against the code, {stale} stale, {len(report['unchecked'])} projects unchecked")
    if args.write:
        store.write_stale(_data(args), report, _tiers(args))
        print("recorded: the next build marks the stale cards and stops introducing them")
    return 1 if stale else 0


def cmd_register(args) -> int:
    edition = _edition(args)
    taken = {p["code"] for p in edition["projects"]}
    print(address.next_code(args.shelf, taken))
    return 0


def cmd_serve(args) -> int:
    from . import server

    edition = _edition(args)
    data = bundle_mod.build_bundle(edition)
    bundle_mod.write_bundle(args.root, data)
    info = {"edition": data["edition"], "cards": len(data["cards"]), "projects": len(data["projects"])}
    httpd, token = server.create_server(os.path.join(args.root, "app"), host=args.host, port=args.port,
                                        progress_path=args.progress, token=args.token, info=info)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{shown}:{httpd.server_address[1]}/" + (f"?token={token}" if token else "")
    print(f"Recurser — {info['cards']} cards on {info['projects']} projects ({info['edition']} edition)")
    print(f"open  {url}")
    if token:
        print("on another device use this computer's network address with the same ?token=... (the link signs that browser in)")
    print("stop  Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


# -- parser -----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recurser", description="Never forget your own projects: decks, matrix, drills.")
    p.add_argument("--root", default=ROOT, help="repository root holding app/ and data/ (default: this checkout)")
    p.add_argument("--data-root", dest="data_root", help="read the tiers from DIR/data/ instead of ROOT/data/")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, tiers=True):
        if tiers:
            sp.add_argument("--tiers", help="comma list of tiers to use (default: every tier present): public,private")
        sp.add_argument("--today", help="pretend today is YYYY-MM-DD (reproducible reports)")

    sp = sub.add_parser("inventory", help="last-accessed table")
    common(sp, tiers=False)
    sp.add_argument("--snapshot", help="read this .tsv/.json snapshot instead of the stored tiers")
    sp.add_argument("--refresh", action="store_true", help="fetch the list from GitHub (gh CLI, or GITHUB_TOKEN)")
    sp.add_argument("--owner", help="GitHub login, for --refresh")
    sp.add_argument("--save", help="with --refresh: write the fetched snapshot to this .json file")
    sp.add_argument("--recent", action="store_true", help="only repositories inside the window")
    sp.add_argument("--days", type=int, default=180, help="window for --recent (default 180)")
    sp.add_argument("--forks", action="store_true", help="include forks in --recent")
    sp.set_defaults(fn=cmd_inventory)

    sp = sub.add_parser("match", help="fuzzy-find a project")
    common(sp)
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=5)
    sp.set_defaults(fn=cmd_match)

    sp = sub.add_parser("validate", help="check a map or cards file")
    sp.add_argument("kind", choices=["map", "cards"])
    sp.add_argument("file")
    sp.add_argument("--repo", action="append", default=[], help="checkout to verify cited paths and quotes against")
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("ingest", help="file analysis output into data/<tier>/")
    sp.add_argument("--manifest", required=True)
    sp.add_argument("--maps", required=True)
    sp.add_argument("--cards", required=True)
    sp.add_argument("--codes", required=True, help="JSON object: slug -> two-digit project code")
    sp.add_argument("--snapshot", help="inventory snapshot (.tsv/.json) to split into the tiers")
    sp.add_argument("--aliases", help="JSON object: slug -> [nicknames]")
    sp.add_argument("--scrub", help='JSON {"all": [...], "public": [...]}: exact identifier strings (a portal number, a cloud project id) replaced by [id] in every tier / in the public tier only')
    sp.add_argument("--as-of", dest="as_of")
    sp.add_argument("--owner")
    sp.set_defaults(fn=cmd_ingest)

    sp = sub.add_parser("build", help="rebuild app/data/bundle.js")
    common(sp)
    sp.add_argument("--single-file", dest="single_file", help="also write the whole app as one .html file")
    sp.add_argument("--fragment", help="also write the page as a fragment for self-wrapping hosts")
    sp.set_defaults(fn=cmd_build)

    sp = sub.add_parser("stats", help="edition counts")
    common(sp)
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("sql", help="query the in-memory database")
    common(sp)
    sp.add_argument("query")
    sp.add_argument("--save", help="also dump the database to this .sqlite file")
    sp.set_defaults(fn=cmd_sql)

    sp = sub.add_parser("xlsx", help="write the fillable matrix workbook")
    common(sp)
    sp.add_argument("out")
    sp.add_argument("--progress", help="history export from the app: sets the Learned column")
    sp.add_argument("--keep-from", dest="keep_from", help="an earlier workbook: carry over everything you filled in")
    sp.set_defaults(fn=cmd_xlsx)

    sp = sub.add_parser("anki", help="write the Anki exports")
    common(sp)
    sp.add_argument("out")
    sp.add_argument("--no-apkg", action="store_true", help="skip the .apkg package")
    sp.add_argument("--progress", help="history export from the app (leaves out cards you set aside)")
    sp.add_argument("--only-learned", dest="only_learned", action="store_true", help="export only cards already Learned: Anki last, once stable")
    sp.set_defaults(fn=cmd_anki)

    sp = sub.add_parser("kb", help="write one Markdown page per project for your vault")
    common(sp)
    sp.add_argument("out")
    sp.add_argument("--progress", help="history export from the app: adds your gap notes to the pages")
    sp.set_defaults(fn=cmd_kb)

    sp = sub.add_parser("verify", help="re-check every card against fresh checkouts; report stale cards")
    common(sp)
    sp.add_argument("--sources-root", dest="sources_root", required=True, help="directory holding one checkout per project slug")
    sp.add_argument("--write", action="store_true", help="record the result so the next build marks stale cards")
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("register", help="print the next free project code on a shelf")
    common(sp)
    sp.add_argument("shelf", type=int, choices=range(10))
    sp.set_defaults(fn=cmd_register)

    sp = sub.add_parser("serve", help="serve the app on localhost")
    common(sp)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--progress", help="history file (default ~/.recurser/progress.json)")
    sp.add_argument("--token", help="access token when listening beyond loopback (default: generated)")
    sp.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)
