#!/usr/bin/env python3
"""Check a recalculated workbook against the edition it was built from (needs LibreOffice for the recalculation).

    python3 tests/check_workbook.py workbook.xlsx <data-root> [YYYY-MM-DD]
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openpyxl import load_workbook  # noqa: E402

from recurser import store  # noqa: E402

path, data_root = sys.argv[1], sys.argv[2]
today = date.fromisoformat(sys.argv[3]) if len(sys.argv) > 3 else date.today()
edition = store.load_edition(data_root, None, today)
wb = load_workbook(path, data_only=True)
fails = []


def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


ov = wb["overview"]
rows = {}
r = 5
while ov.cell(r, 2).value not in (None, "TOTAL"):
    rows[ov.cell(r, 1).value] = [ov.cell(r, c).value for c in range(1, 14)]
    r += 1
total_row = r
per_project = {}
for c in edition["cards"]:
    per_project[c["code"][:2]] = per_project.get(c["code"][:2], 0) + 1
check("overview lists every project", set(rows) == {p["code"] for p in edition["projects"]}, str(sorted(rows)))
check("overview Cards = cards per project", all(rows[k][3] == v for k, v in per_project.items()), str({k: rows[k][3] for k in rows}))
check("overview TOTAL = edition cards", ov.cell(total_row, 4).value == len(edition["cards"]), str(ov.cell(total_row, 4).value))
check("overview row check = TOTAL", ov.cell(total_row + 1, 4).value == len(edition["cards"]), str(ov.cell(total_row + 1, 4).value))
tiers_ok = all(isinstance(v[7], (int, float)) and v[8] in ("hot", "warm", "cooling", "cold", "frozen") for v in rows.values())
check("overview Days since and Tier computed", tiers_ok, str({k: (v[7], v[8]) for k, v in rows.items()}))
check("overview Retention between 0 and 1", all(isinstance(v[9], (int, float)) and 0 <= v[9] <= 1 for v in rows.values()))
ranks = sorted(v[10] for v in rows.values() if isinstance(v[10], (int, float)))
check("overview Priority ranks from 1", bool(ranks) and ranks[0] == 1, str(ranks))
cat_total = sum(ov.cell(total_row + 5 + d, 4).value or 0 for d in range(10))
check("category table sums to all cards", cat_total == len(edition["cards"]), str(cat_total))
lvl_total = sum(ov.cell(total_row + 17 + lv, 4).value or 0 for lv in range(1, 5))
check("level table sums to all cards", lvl_total == len(edition["cards"]), str(lvl_total))

mx = wb["matrix"]
n = len(edition["cards"])
learned_cells = [mx.cell(i, 1).value for i in range(2, n + 2)]
check("matrix SUM row = number of Learned flags", mx.cell(n + 3, 1).value == sum(learned_cells), f"{mx.cell(n + 3, 1).value} vs {sum(learned_cells)}")
pool = [i for i in range(2, n + 2) if isinstance(mx.cell(i, 18).value, (int, float))]
unfiltered = wb["drill"]["B1"].value in (None, "") and wb["drill"]["B2"].value == 4
if unfiltered:
    check("matrix Pool lists every row when the drill is unfiltered", len(pool) == n, str(len(pool)))
else:
    want = [i for i in range(2, n + 2) if (wb["drill"]["B1"].value in (None, "") or mx.cell(i, 3).value == wb["drill"]["B1"].value)
            and mx.cell(i, 13).value <= wb["drill"]["B2"].value]
    check("matrix Pool follows the drill filter", pool == want, f"{len(pool)} rows")

dr = wb["drill"]
pick = dr["A6"].value
check("drill draws a row from the pool", isinstance(pick, (int, float)) and int(pick) in pool, str(pick))
if isinstance(pick, (int, float)):
    check("drill question comes from that row", dr["C6"].value == mx.cell(int(pick), 4).value)
    check("drill answer comes from that row", dr["E6"].value == mx.cell(int(pick), 5).value)
check("drill names the project of the day", dr["C13"].value in [v[1] for v in rows.values()], str(dr["C13"].value))

st = wb["sit"]
typed = [i for i in range(5, n + 5) if st.cell(i, 6).value not in (None, "")]
blank = [i for i in range(5, n + 5) if st.cell(i, 6).value in (None, "")]
check("sit hides the answer until an attempt is typed", all(st.cell(i, 7).value == "(answer first)" for i in blank), f"{len(blank)} blank rows")
by_id = {c["id"]: c for c in edition["cards"]}
for i in typed:
    card = by_id[st.cell(i, 11).value]
    want = card.get("expected_output") if card["type"] == "output" else None
    got = st.cell(i, 7).value
    check(f"sit reveals the reference once typed (row {i})", got not in (None, "", "(answer first)") and (want is None or str(got) == str(want)), str(got)[:60])
tr = wb["traces"]
steps = sum(len(c.get("steps") or []) for c in edition["cards"] if c["type"] == "trace")
check("traces has one row per step", sum(1 for i in range(5, tr.max_row + 1) if tr.cell(i, 9).value) == steps, str(steps))
check("traces hides each step until an attempt is typed", all(tr.cell(i, 7).value == "(answer first)" for i in range(5, tr.max_row + 1) if tr.cell(i, 9).value and tr.cell(i, 6).value in (None, "")))
tx = wb["taxonomy"]
grid_total = 0
for i in range(31, 31 + len(edition["projects"])):
    grid_total += sum(tx.cell(i, 3 + d).value or 0 for d in range(10))
check("taxonomy grid sums to all cards", grid_total == len(edition["cards"]), str(grid_total))
print("\n" + ("all workbook checks passed" if not fails else f"{len(fails)} workbook check(s) failed"))
sys.exit(1 if fails else 0)
