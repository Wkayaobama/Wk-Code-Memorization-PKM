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
    only = getattr(args, "only", None)
    if only:
        try:
            slugs = store.resolve_projects(edition, [n for n in only.split(",") if n.strip()])
        except KeyError as exc:
            unknown = str(exc.args[0]).split(", ")
            hints = []
            for name in unknown:
                near = fuzzy.rank(name, edition["projects"], limit=2)
                hints.append(f"{name!r}" + (" (did you mean " + " or ".join(h["item"]["slug"] for h in near) + "?)" if near else ""))
            sys.exit("--only: unknown project " + ", ".join(hints) + ". Use slugs, repository names or two-digit codes.")
        edition = store.restrict(edition, slugs)
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


def _settings(args):
    from . import config as config_mod

    try:
        return config_mod.load(getattr(args, "config", None))
    except config_mod.ConfigError as exc:
        sys.exit(str(exc))


def _configured(args) -> bool:
    """True when a settings file is in play (a flag, the environment, or the ConfigMap mount)."""
    from . import config as config_mod

    return bool(getattr(args, "config", None) or os.environ.get("RECURSER_CONFIG") or os.path.exists(config_mod.DEFAULT_PATH))


def _deployment_edition(args, settings) -> dict:
    """The edition this server holds: --only wins, else access.only from the settings."""
    edition = _edition(args)
    only = settings["access"]["only"]
    if only and not getattr(args, "only", None):
        try:
            edition = store.restrict(edition, store.resolve_projects(edition, only))
        except KeyError as exc:
            sys.exit(f"access.only: unknown project {exc.args[0]}")
    return edition


def _client(settings):
    """An Anthropic client when the feature can run here, else None with the reason."""
    from . import assistant

    if not settings["llm"]["enabled"]:
        return None, "turned off in the settings (llm.enabled)"
    try:
        return assistant.make_client(timeout=float(settings["llm"]["timeout_seconds"])), ""
    except assistant.AssistantUnavailable as exc:
        return None, str(exc)


def cmd_serve(args) -> int:
    from . import server

    def leave(signum, frame):     # as the first process of a container, a stop request must be handled explicitly
        raise SystemExit(0)

    try:
        import signal

        signal.signal(signal.SIGTERM, leave)
    except ValueError:            # not the main thread: nothing to install
        pass
    settings = _settings(args)
    configured = _configured(args)
    edition = _deployment_edition(args, settings)
    host = args.host or (settings["server"]["host"] if configured else "127.0.0.1")
    port = args.port if args.port is not None else (settings["server"]["port"] if configured else 8765)
    users_path = args.users or settings["access"]["users_file"]
    client, why_not = _client(settings)
    try:
        if users_path:
            site = server.Site(edition, users=server.load_users(users_path), token=None, settings=settings, client=client,
                               user_header=args.user_header or settings["access"]["trust_user_header"],
                               progress_dir=args.progress_dir or (settings["server"]["progress_dir"] if configured
                                                                  else os.path.join(os.path.expanduser("~"), ".recurser", "team")))
        else:
            token = args.token
            if not server.is_loopback(host) and not token:
                # A generated link is shown on a terminal, for a phone on the same network.  Anywhere else
                # (a container, a service) it would land in a log: there, name the people or the token.
                if not sys.stdout.isatty():
                    sys.exit("Refusing to listen beyond this machine without knowing who may come in: pass --users users.json "
                             "(a personal link per person) or --token <value>.")
                import secrets

                token = secrets.token_urlsafe(18)
                generated = True
            site = server.Site(edition, progress_path=args.progress, token=token if not server.is_loopback(host) or token else None,
                               settings=settings, client=client)
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot start: {exc}")
    httpd, token = server.create_server(os.path.join(args.root, "app"), host=host, port=port, site=site)
    if token and not locals().get("generated"):
        token = "<the token you passed>"                      # a token given on purpose is never echoed
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    base = f"http://{shown}:{httpd.server_address[1]}/"
    print(f"Recurser: {len(edition['cards'])} cards on {len(edition['projects'])} projects ({edition['edition']} edition)")
    print("assistant  " + (f"on, {settings['llm']['model']} through the Anthropic SDK" if client else f"off: {why_not}"))
    if site.team:
        print(f"team       {len(site.users)} people, each with a personal link {base}?token=<their token> and a history of their own")
        for user in site.users:
            shown_projects = "every project" if user.get("projects") == "*" else f"{len(user['_slugs'])} projects"
            print(f"           {user['name']}: {shown_projects}")
    else:
        print(f"open       {base}" + (f"?token={token}" if token else ""))
        if token:
            print("           on another device use this computer's network address with the same ?token=... (the link signs that browser in)")
    print("stop       Ctrl+C", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def cmd_ask(args) -> int:
    """Retrieve the passages for a question and, when a credential is present, answer from them."""
    from . import assistant, retrieval

    settings = _settings(args)
    edition = _deployment_edition(args, settings)
    slugs = None
    if args.project:
        try:
            slugs = store.resolve_projects(edition, [args.project])
        except KeyError as exc:
            sys.exit(f"unknown project {exc.args[0]}")
    found = retrieval.Index(edition).search(args.question, k=settings["retrieval"]["top_k"], slugs=slugs,
                                            per_project=settings["retrieval"]["per_project"])
    if not found:
        print("No passage matches this question.")
        return 1
    print(_table([{"score": p["score"], "passage": p["title"][:92], "where": p["path"][:44]} for p in found], ["score", "passage", "where"]))
    if args.no_llm:
        return 0
    client, why_not = _client(settings)
    if client is None:
        print(f"\n(no answer: {why_not})", file=sys.stderr)
        return 0
    try:
        result = assistant.ask(args.question, found, client=client, model=settings["llm"]["model"], effort=settings["llm"]["effort"],
                               max_tokens=settings["llm"]["max_tokens"], fallbacks=settings["llm"]["fallbacks"],
                               answer_seconds=settings["llm"]["answer_seconds"])
    except assistant.AssistantError as exc:
        sys.exit(f"the assistant failed: {exc}")
    titles = {p["id"]: p["title"] for p in found}
    print("\n" + result["text"].strip())
    if result["sources"]:
        print("\nRests on:")
        for pid in result["sources"]:
            print("  - " + titles.get(pid, pid))
    return 0


def cmd_users(args) -> int:
    """Manage the users file of a team server: who may open the page and which projects each person sees."""
    import secrets

    path = args.file
    data = {"users": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            sys.exit(f"{path} is not a users file")
    users = data["users"]
    if args.action == "list":
        print(_table([{"name": u["name"], "projects": "every project" if u.get("projects") == "*" else ", ".join(u.get("projects") or []) or "(none)"}
                      for u in users], ["name", "projects"]) if users else "(no users yet)")
        return 0
    from . import server

    if not args.name or not server.USER_NAME.match(args.name):
        sys.exit("give a name of lower-case letters, digits, '.', '_' or '-'")
    existing = [u for u in users if u["name"] == args.name]
    if args.action == "remove":
        if not existing:
            sys.exit(f"no user named {args.name}")
        data["users"] = [u for u in users if u["name"] != args.name]
    else:
        if args.all == bool(args.projects):
            sys.exit("say which projects: --projects a,b or --all")
        projects = "*" if args.all else [n.strip() for n in args.projects.split(",") if n.strip()]
        if projects != "*":
            try:
                projects = store.resolve_projects(_edition(args), projects)
            except KeyError as exc:
                sys.exit(f"unknown project {exc.args[0]}")
        if existing:                                   # same person, new project list: the link keeps working
            existing[0]["projects"] = projects
            token = existing[0]["token"]
        else:
            token = secrets.token_urlsafe(24)
            users.append({"name": args.name, "token": token, "projects": projects})
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)      # the file holds sign-in tokens
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)
    if args.action == "remove":
        print(f"removed {args.name}; restart the server for it to take effect")
    else:
        print(f"{args.name}: " + ("every project" if projects == "*" else f"{len(projects)} projects"))
        print(f"personal link: <address of the server>/?token={token}")
        print("send it over a private channel; restart the server for the change to take effect")
    return 0


def k8s_files(namespace: str = "recurser", name: str = "team", image: str = "", pull_secret: str = "") -> dict[str, str]:
    """The files under deploy/k8s/, as text.  ``recurser k8s write`` saves them; a test keeps them current."""
    import yaml

    from . import k8s

    image = image or k8s.DEFAULT_IMAGE
    ex = k8s.example(name, namespace, image, pull_secret)
    config_map = dict(ex["config"], data={k8s.CONFIG_KEY: yaml.safe_dump(ex["config"]["data"][k8s.CONFIG_KEY], sort_keys=False)})
    plain = k8s.render(name, namespace, ex["app"]["spec"])
    header = "# Generated by `python3 -m recurser k8s write`. Edit recurser/k8s.py, not this file.\n"
    secrets = f"""# Create the two Secrets with kubectl, so that no credential is ever written to a file in git:
#
#   kubectl -n {namespace} create secret generic {name}-anthropic --from-literal={k8s.API_KEY_ENV}=<your key>
#   python3 -m recurser users add <name> --projects <a,b> --file users.json     # once per person
#   kubectl -n {namespace} create secret generic {name}-users --from-file={k8s.USERS_KEY}=users.json
#
# The pod reads the first one as an environment variable (secretKeyRef) and the second as a file
# ({k8s.SECRETS_DIR}/{k8s.USERS_KEY}).  Shown here for reference only, with placeholders:
apiVersion: v1
kind: Secret
metadata:
  name: {name}-anthropic
  namespace: {namespace}
type: Opaque
stringData:
  {k8s.API_KEY_ENV}: "<set with kubectl, never commit a real key>"
"""
    return {
        "00-namespace.yaml": header + k8s.to_yaml([ex["namespace"]]),
        "01-crd.yaml": header + k8s.to_yaml([k8s.crd()]),
        "02-controller.yaml": header + k8s.to_yaml(k8s.controller_manifests(namespace, image, pull_secret)),
        "03-config.yaml": header + "# Layer 2: the settings, mounted in the pod at " + f"{k8s.CONFIG_DIR}/{k8s.CONFIG_KEY}.\n" + k8s.to_yaml([config_map]),
        "04-secrets.example.yaml": secrets,
        "05-recurserapp.yaml": header + "# Layer 5: the one object you declare. The controller generates the Deployment and the Service from it.\n" + k8s.to_yaml([ex["app"]]),
        "plain/recurser.yaml": header + "# The same Deployment, Service, volume claim and NetworkPolicy without the controller:\n"
                               "# apply 00, 03 and the two Secrets, then this file.\n" + k8s.to_yaml(plain),
    }


def cmd_k8s(args) -> int:
    from . import k8s

    files = k8s_files(args.namespace, args.name, args.image, args.image_pull_secret or "")
    if args.what == "write":
        for rel, text in files.items():
            path = os.path.join(args.out, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            print("wrote", path)
        return 0
    wanted = {"crd": ["01-crd.yaml"], "controller": ["02-controller.yaml"], "example": ["03-config.yaml", "04-secrets.example.yaml", "05-recurserapp.yaml"],
              "plain": ["plain/recurser.yaml"]}[args.what]
    print("---\n".join(files[rel] for rel in wanted), end="")
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
            sp.add_argument("--only", help="comma list of projects (slug, repository name or two-digit code): build for these only, "
                                           "with the names of the other private repositories removed")
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

    sp = sub.add_parser("serve", help="serve the app: on localhost for you, or to a team")
    common(sp)
    sp.add_argument("--host", help="default 127.0.0.1, or server.host from the settings file")
    sp.add_argument("--port", type=int, help="default 8765, or server.port from the settings file")
    sp.add_argument("--progress", help="history file when serving one person (default ~/.recurser/progress.json)")
    sp.add_argument("--token", help="access token when listening beyond loopback (default: generated)")
    sp.add_argument("--config", help="settings file (YAML or JSON); default $RECURSER_CONFIG or /app/config/config.yaml when present")
    sp.add_argument("--users", help="users file: serve a team, each person with a personal link, a project list and a history")
    sp.add_argument("--progress-dir", dest="progress_dir", help="folder for the team's history files")
    sp.add_argument("--user-header", dest="user_header", help="trust this header for the person's name (only behind an authenticating proxy)")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("ask", help="ask the decks a question: the passages found and, with a credential, an answer")
    common(sp)
    sp.add_argument("question")
    sp.add_argument("--project", help="limit the search to one project")
    sp.add_argument("--no-llm", dest="no_llm", action="store_true", help="show the passages only")
    sp.add_argument("--config", help="settings file (YAML or JSON)")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("k8s", help="print or write the Kubernetes files: crd, controller, example, plain, write")
    sp.add_argument("what", choices=["crd", "controller", "example", "plain", "write"])
    sp.add_argument("--namespace", default="recurser")
    sp.add_argument("--name", default="team", help="name of the RecurserApp and of its Service")
    sp.add_argument("--image", default="", help="the image built from this version of the tool (default: a placeholder with this version as its tag)")
    sp.add_argument("--image-pull-secret", dest="image_pull_secret", help="name of a registry Secret, for an image in a private registry")
    sp.add_argument("--out", default=os.path.join(ROOT, "deploy", "k8s"), help="folder for `write`")
    sp.set_defaults(fn=cmd_k8s)

    sp = sub.add_parser("users", help="manage the users file of a team server")
    common(sp)
    sp.add_argument("action", choices=["add", "list", "remove"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("--file", default="users.json", help="the users file (default ./users.json); keep it out of git")
    sp.add_argument("--projects", help="comma list of projects this person may open")
    sp.add_argument("--all", action="store_true", help="this person may open every project")
    sp.set_defaults(fn=cmd_users)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "serve":           # only the server stays up: see __main__ for the handler taken away here
        import signal

        try:
            if getattr(signal.getsignal(signal.SIGTERM), "__name__", "") == "_stopped_early":
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
        except ValueError:                # not the main thread
            pass
    return args.fn(args)
