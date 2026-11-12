#!/usr/bin/env python3
"""Fill yellow cells the way the owner would, then regenerate with --keep-from and prove nothing typed was lost.

    python3 tests/roundtrip_workbook.py <filled-out.xlsx> <regenerated.xlsx> <data-root> [YYYY-MM-DD]   (step 2: compare)
    python3 tests/roundtrip_workbook.py --fill <fresh.xlsx> <filled-out.xlsx>                           (step 1: fill)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openpyxl import load_workbook  # noqa: E402

from recurser import xlsx  # noqa: E402

if sys.argv[1] == "--fill":
    wb = load_workbook(sys.argv[2])
    mx, st, tr, ix, dr = wb["matrix"], wb["sit"], wb["traces"], wb["index"], wb["drill"]
    mx["A2"], mx["A4"] = 1, 1
    mx["I2"], mx["J3"] = "my own wording of this card", "tried a pun on 'tide', too weak"
    mx["U2"], mx["V2"] = "add the second gauge vendor", "!!"
    out_row = next(i for i in range(5, st.max_row + 1) if st.cell(i, 3).value == "output")
    st.cell(out_row, 6).value = "91.4" if "ft" in (st.cell(out_row, 5).value or "") else "wrong guess"
    plain_row = next(i for i in range(5, st.max_row + 1) if st.cell(i, 3).value == "recall")
    st.cell(plain_row, 6).value = "it loads tide gauge files"
    st.cell(plain_row, 9).value = 0
    st.cell(plain_row, 10).value = "forgot: one batch per station and day"
    tr["F5"] = "find the new csv files"
    ix["C5"] = "counts doubled · merge outside its transaction · wrap it"
    dr["B1"] = st.cell(5, 2).value
    dr["B2"] = 2
    wb.save(sys.argv[3])
    print("filled", sys.argv[3], "| output row", out_row, "| recall row", plain_row)
    sys.exit(0)

filled, regenerated = xlsx.read_fillins(sys.argv[1]), xlsx.read_fillins(sys.argv[2])
ok = True
for key in ("matrix", "sit", "traces", "index", "drill", "backlog"):
    same = filled[key] == regenerated[key]
    ok &= same
    print(("ok   " if same else "FAIL ") + f"{key}: {len(filled[key])} fill-in group(s) carried over")
    if not same:
        print("   before:", filled[key], "\n   after: ", regenerated[key])
sys.exit(0 if ok else 1)
