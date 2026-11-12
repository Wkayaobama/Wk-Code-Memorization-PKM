#!/usr/bin/env python3
"""A team server on the fictional demo edition, with a local stand-in for the Anthropic API.

    python3 tests/team_demo_server.py [port]

Two people: "owner" sees both demo projects, "sam" only tide-loader.  No credential is needed and
nothing leaves this machine: answers come from tests/fake_anthropic.py.  Used by
tests/team_browser_check.js; also a quick way to look at the team mode.
"""
import os
import subprocess
import sys
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import anthropic  # noqa: E402
from fake_anthropic import FakeAnthropic  # noqa: E402

from recurser import config, server, store  # noqa: E402

DEMO = os.path.join(ROOT, "tests", "fixtures", "demo")
SIGN_IN = {"owner": "owner-" + "token-0123456789abcdef", "sam": "sam-" + "token-0123456789abcdefgh"}    # demo values, not credentials
USERS = [dict(name="owner", token=SIGN_IN["owner"], projects="*"), dict(name="sam", token=SIGN_IN["sam"], projects=["tide-loader"])]

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    if not os.path.exists(os.path.join(DEMO, "data", "public", "projects.json")):
        subprocess.run([sys.executable, os.path.join(ROOT, "tests", "make_demo.py"), DEMO], check=True, capture_output=True)
    api = FakeAnthropic()
    client = anthropic.Anthropic(api_key="test-key", base_url=api.url, max_retries=0)
    edition = store.load_edition(DEMO, ["public"], date(2026, 11, 12))
    site = server.Site(edition, users=USERS, progress_dir=tempfile.mkdtemp(prefix="recurser-team-"), client=client, settings=config.merge(None))
    httpd, _ = server.create_server(os.path.join(ROOT, "app"), port=port, site=site)
    for user in USERS:
        print(f"{user['name']}: http://127.0.0.1:{port}/?token={user['token']}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
