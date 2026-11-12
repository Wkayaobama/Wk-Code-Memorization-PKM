"""The fillable matrix workbook — the same family as the owner's Millenium matrices.

One script generates it from the edition; it is never hand-maintained and never
the master.  Yellow cells are yours to fill; everything else is regenerated.
On regeneration ``--keep-from old.xlsx`` carries every yellow cell over by card
id, so nothing you typed is lost.

Tabs::

    instructions  step | where | what to do, the Learned rule, the Anki route
    overview      one row per project with live roll-ups (cards, learned, days since, tier, retention, priority)
    matrix        one row per card: Learned flag, code, question, answer, anchor, confusables, bench, backlog
    sit           attempt first: the answer appears only once something is typed
    drill         press F9: a random card, optionally filtered by project and level
    traces        one row per step of every ordered chain: context -> your next step
    taxonomy      how an address reads; project x category grid
    index         per project, the five "my index" slots

Requires ``openpyxl`` (``pip install openpyxl``).
"""
from __future__ import annotations

import os
import re
from datetime import datetime

try:
    from . import address, inventory, schema
except ImportError:  # pragma: no cover
    import address, inventory, schema  # type: ignore

FONT = "Arial"
HEAD_FILL = "DDEBF7"
INPUT_FILL = "FFFF00"
ZEBRA_FILL = "F2F2F2"
RATING_FILL = {0: "FFC7CE", 1: "FFEB9C", 2: "C6EFCE", 3: "C6EFCE"}
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

MATRIX_HEADERS = [
    "Learned (0/1)", "Code", "Project", "Question", "Answer", "Anchor (mental image)", "Example / expected output",
    "Attention doublon / confusables", "Non retenu mais convient", "Autre idée difficile à utiliser", "Category",
    "Type", "Level", "Concept", "Source", "Evidence", "Card ID", "Pool", "", "", "A FAIRE", "!!!!", "done",
]
MATRIX_WIDTHS = [11, 7, 26, 52, 52, 40, 40, 40, 30, 30, 9, 10, 7, 28, 34, 44, 26, 7, 3, 3, 30, 8, 8]
INDEX_SLOTS = [
    "▢ trap I hit → symptom · cause · fix",
    "▢ command I forgot → the flag, the order",
    "▢ numbers to re-derive → how, in one line",
    "▢ where to look → file · grader line · fixture",
    "▢ the sentence — if I could carry one line into the exam for this project, it is:",
]
INSTRUCTIONS = [
    (1, "read", "Yellow cells are yours to fill. Everything else is regenerated from the project decks: edit a card in data/<tier>/cards/<project>.json, never here."),
    (2, "overview", "Start with the project whose Priority is 1: it is the one fading fastest. Days since counts from your last push to any branch."),
    (3, "matrix", "One row per card at its fixed address (Code = project shelf, project slot, knowledge shelf, rank). Filter on Project, then read down the ranks: rank 0 first."),
    (4, "sit", "Attempt first: type your answer in the yellow cell and the reference appears beside it. Mark Got it 1 or 0, and write the gap when it is 0. The gap is the thing to write down."),
    (5, "drill", "Press F9 (recalculate) for a random card. Say the answer aloud BEFORE reading the answer column. Set a project or a highest level in the two yellow cells to narrow the draw."),
    (6, "traces", "For every ordered chain: read the two steps of context, type the next step, then compare. [Beginning] marks the start, □ the last step."),
    (7, "Learned", "Set Learned to 1 only after you recalled the card unaided at least 24 hours after the previous review. The SUM under the column is your progress bar."),
    (8, "bench", "A wording you prefer goes to 'Non retenu mais convient'; an idea you dropped goes to 'Autre idée difficile à utiliser' with its reason. Nothing is re-evaluated from scratch."),
    (9, "anki (LAST)", "Export only what is stable: python -m recurser anki out_dir writes one semicolon CSV per project, one tab-separated file with import headers, and recurser.apkg."),
    ("", "", ""),
    ("example", "matrix", "Code 2150 reads: shelf 2, project 1, category 5 (runtime & orchestration), rank 0, the most fundamental card on that shelf."),
    ("example", "sit", "Your answer: 'merge on batch_key so reruns replace' → the reference appears → Got it: 1."),
    ("why", "addresses", "An address is issued once and never renumbered, so what you learned stays where you left it."),
    ("why", "attempt first", "Recognising an answer feels like knowing it. Typing first is the only honest test."),
    ("legend", "yellow", "Fill-in cell. On regeneration pass --keep-from <this file> and every yellow cell is carried over by card id."),
]


class WorkbookLocked(RuntimeError):
    pass


def _clean(value) -> str:
    return _ILLEGAL.sub(" ", "" if value is None else str(value))


def _first_source(card: dict) -> str:
    refs = card.get("source_refs") or []
    if refs:
        r = refs[0]
        return r["path"] + (f":{r['line']}" if r.get("line") else "")
    src = card.get("source") or []
    return src[0] if src else ""


def _payload(card: dict) -> tuple[str, str]:
    """(answer cell, example / expected-output cell) for one card."""
    answer = card.get("answer", "")
    if card.get("type") == "trace" and card.get("steps"):
        answer = " → ".join(card["steps"])
    if card.get("type") == "output":
        return answer, card.get("expected_output", "")
    return answer, card.get("example", "")


def _question(card: dict) -> str:
    q = card.get("question", "")
    if card.get("type") == "cloze" and card.get("cloze"):
        q += "  " + schema.CLOZE_RE.sub("[...]", card["cloze"])
    if card.get("type") == "output" and card.get("code_snippet"):
        q += "  " + " / ".join(line.strip() for line in card["code_snippet"].splitlines() if line.strip())
    return q


def read_fillins(path: str) -> dict:
    """Everything the owner typed into an earlier workbook, keyed so it can be re-injected."""
    from openpyxl import load_workbook

    wb = load_workbook(path)  # formulas as written; we only read literal cells
    out = {"matrix": {}, "backlog": [], "sit": {}, "traces": {}, "index": {}, "drill": {}}

    def literal(v):
        return None if v is None or (isinstance(v, str) and v.startswith("=")) else v

    if "matrix" in wb.sheetnames:
        ws = wb["matrix"]
        for row in ws.iter_rows(min_row=2, values_only=True):
            row = list(row) + [None] * 23
            cid = row[16]
            if isinstance(cid, str) and ":" in cid:
                keep = {"learned": literal(row[0]), "bench": literal(row[8]), "rejects": literal(row[9])}
                if any(v not in (None, "", 0) for v in keep.values()):
                    out["matrix"][cid] = keep
            if any(literal(v) not in (None, "") for v in row[20:23]):
                out["backlog"].append([literal(v) for v in row[20:23]])
    if "sit" in wb.sheetnames:
        for row in wb["sit"].iter_rows(min_row=5, values_only=True):
            row = list(row) + [None] * 11
            if row[10] and any(literal(row[i]) not in (None, "") for i in (5, 8, 9)):
                out["sit"][row[10]] = {"answer": literal(row[5]), "got": literal(row[8]), "gap": literal(row[9])}
    if "traces" in wb.sheetnames:
        for row in wb["traces"].iter_rows(min_row=5, values_only=True):
            row = list(row) + [None] * 9
            if row[8] and literal(row[5]) not in (None, ""):
                out["traces"][f"{row[8]}#{row[3]}"] = literal(row[5])
    if "index" in wb.sheetnames:
        for row in wb["index"].iter_rows(min_row=5, values_only=True):
            row = list(row) + [None] * 8
            if row[7] and any(literal(v) not in (None, "") for v in row[2:7]):
                out["index"][row[7]] = [literal(v) for v in row[2:7]]
    if "drill" in wb.sheetnames:
        out["drill"] = {"project": literal(wb["drill"]["B1"].value), "level": literal(wb["drill"]["B2"].value)}
    return out


def build_workbook(edition: dict, out_path: str, *, progress: dict | None = None, keep_from: str | None = None) -> str:
    """Write the workbook and verify it.  Raises on any mismatch between sheet and edition."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    folder, name = os.path.split(os.path.abspath(out_path))
    if os.path.exists(os.path.join(folder, "~$" + name)):
        raise WorkbookLocked(f"{name} is open in a spreadsheet program (lock file ~${name} exists). Close it and run again.")

    keep = read_fillins(keep_from) if keep_from else {"matrix": {}, "backlog": [], "sit": {}, "traces": {}, "index": {}, "drill": {}}
    projects, cards = edition["projects"], edition["cards"]
    title_of, seen_titles = {}, set()
    for p in projects:  # roll-ups key on the project title, so it must be unique
        t = _clean(p.get("title") or p["slug"])
        if t in seen_titles:
            t = f"{t} ({p['code']})"
        seen_titles.add(t)
        title_of[p["slug"]] = t
    states = (progress or {}).get("cards") or {}
    last_rating = {}
    for entry in (progress or {}).get("log") or []:
        if not entry.get("c"):
            last_rating[entry.get("id")] = entry.get("r")

    regular, bold = Font(name=FONT, size=10), Font(name=FONT, size=10, bold=True)
    hint = Font(name=FONT, size=10, italic=True, color="808080")
    head_fill = PatternFill("solid", fgColor=HEAD_FILL)
    input_fill = PatternFill("solid", fgColor=INPUT_FILL)
    zebra = PatternFill("solid", fgColor=ZEBRA_FILL)
    wrap = Alignment(wrap_text=True, vertical="top")
    top = Alignment(vertical="top")

    def text(ws, row, col, value, *, font=regular, fill=None, align=wrap):
        """Write a literal string: a card that starts with '=' must never become a formula."""
        cell = ws.cell(row, col)
        cell.value = _clean(value)
        cell.data_type = "s"
        cell.font, cell.alignment = font, align
        if fill:
            cell.fill = fill
        return cell

    def put(ws, row, col, value, *, font=regular, fill=None, align=top, fmt=None):
        """Write a number, a date or a formula."""
        cell = ws.cell(row, col, value)
        cell.font, cell.alignment = font, align
        if fill:
            cell.fill = fill
        if fmt:
            cell.number_format = fmt
        return cell

    def header(ws, row, labels, widths=None):
        for j, label in enumerate(labels, start=1):
            text(ws, row, j, label, font=bold, fill=head_fill if label else None)
        for j, w in enumerate(widths or [], start=1):
            ws.column_dimensions[get_column_letter(j)].width = w

    wb = Workbook()
    wb.calculation.fullCalcOnLoad = True

    # ---- instructions -------------------------------------------------------
    ws = wb.active
    ws.title = "instructions"
    header(ws, 1, ["step", "where", "what to do"], [10, 16, 120])
    for i, (step, where, what) in enumerate(INSTRUCTIONS, start=2):
        text(ws, i, 1, step)
        text(ws, i, 2, where)
        text(ws, i, 3, what)
    row = len(INSTRUCTIONS) + 3
    text(ws, row, 1, "edition", font=bold)
    text(ws, row, 3, f"{edition.get('edition', 'public')} edition · {len(cards)} cards on {len(projects)} projects · inventory as of {edition.get('as_of', '')}"
         + (" · contains decks built from private repositories: keep this file to yourself" if edition.get("edition") == "private" else ""))
    ws.freeze_panes = "A2"

    # ---- matrix -------------------------------------------------------------
    ws = wb.create_sheet("matrix")
    header(ws, 1, MATRIX_HEADERS, MATRIX_WIDTHS)
    n = len(cards)
    last = n + 1
    flag = DataValidation(type="list", formula1='"0,1"', allow_blank=True)
    ws.add_data_validation(flag)
    orphans = dict(keep["matrix"])
    for i, c in enumerate(cards, start=2):
        kept = orphans.pop(c["id"], {})
        state = states.get(c["id"]) or {}
        learned = 1 if (kept.get("learned") == 1 or state.get("learned")) else 0
        answer, example = _payload(c)
        put(ws, i, 1, learned, fill=input_fill)
        code_cell = text(ws, i, 2, c["code"], align=top)
        if c["id"] in last_rating and last_rating[c["id"]] in RATING_FILL:
            code_cell.fill = PatternFill("solid", fgColor=RATING_FILL[last_rating[c["id"]]])
        text(ws, i, 3, title_of[c["slug"]])
        text(ws, i, 4, _question(c))
        text(ws, i, 5, answer)
        text(ws, i, 6, c.get("anchor", ""))
        text(ws, i, 7, example)
        text(ws, i, 8, c.get("confusables", ""))
        text(ws, i, 9, kept.get("bench") or "", fill=input_fill)
        text(ws, i, 10, kept.get("rejects") or "", fill=input_fill)
        put(ws, i, 11, c["category"])
        text(ws, i, 12, c["type"], align=top)
        put(ws, i, 13, c["level"])
        text(ws, i, 14, c.get("concept", ""))
        text(ws, i, 15, _first_source(c))
        text(ws, i, 16, c.get("evidence_quote", ""))
        text(ws, i, 17, c["id"], align=top)
        put(ws, i, 18, f'=IF(AND(OR(drill!$B$1="",C{i}=drill!$B$1),M{i}<=drill!$B$2),ROW(),"")')
    if n:
        flag.add(f"A2:A{last}")
    put(ws, last + 2, 1, f"=SUM(A2:A{last})", font=bold)
    text(ws, last + 2, 2, f"learned / {n}", font=bold, align=top)
    backlog = keep["backlog"] or [["next project to add to the deck", "", ""], ["card that needs a better anchor", "", ""]]
    for i, entry in enumerate(backlog, start=2):
        for j, value in enumerate(entry):
            text(ws, i, 21 + j, value or "", font=regular if keep["backlog"] else hint, fill=input_fill)
    for i in range(len(backlog) + 2, 22):
        for j in range(3):
            ws.cell(i, 21 + j).fill = input_fill
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:Q{max(last, 2)}"

    # ---- overview -----------------------------------------------------------
    ws = wb.create_sheet("overview", 1)
    text(ws, 1, 1, "Projects by how much of each is still in memory", font=Font(name=FONT, size=12, bold=True), align=top)
    text(ws, 2, 1, "Retention is an estimate, not a measurement: 0.5 ^ (days since last push / (21 × (1 + 9 × learned share))). Constants: 21-day half-life unrehearsed, ×10 when fully learned (recurser/inventory.py).", font=hint)
    ws.merge_cells("A2:M2")
    heads = ["Code", "Project", "Repository", "Cards", "Learned", "Progress", "Last push", "Days since", "Tier", "Retention (estimate)", "Priority", "Visibility", "Branch analysed"]
    header(ws, 4, heads, [7, 34, 28, 8, 9, 10, 12, 11, 10, 18, 9, 11, 40])
    first = 5
    end = first + len(projects) - 1
    for i, p in enumerate(projects, start=first):
        text(ws, i, 1, p["code"], align=top)
        text(ws, i, 2, title_of[p["slug"]])
        text(ws, i, 3, p["repo"])
        put(ws, i, 4, f"=COUNTIF(matrix!$C:$C,B{i})")
        put(ws, i, 5, f"=SUMIF(matrix!$C:$C,B{i},matrix!$A:$A)")
        put(ws, i, 6, f"=IF(D{i}=0,0,E{i}/D{i})", fmt="0%")
        pushed = inventory.parse_ts(p["pushed_at"]).date() if p.get("pushed_at") else None
        put(ws, i, 7, pushed, fmt="yyyy-mm-dd")
        put(ws, i, 8, f'=IF(G{i}="","",INT(TODAY()-G{i}))')
        put(ws, i, 9, f'=IF(H{i}="","",IF(H{i}<=14,"hot",IF(H{i}<=45,"warm",IF(H{i}<=120,"cooling",IF(H{i}<=365,"cold","frozen")))))')
        put(ws, i, 10, f'=IF(H{i}="","",0.5^(H{i}/({inventory.BASE_HALF_LIFE_DAYS:g}*(1+{inventory.MASTERY_GAIN:g}*F{i}))))', fmt="0%")
        put(ws, i, 11, f'=IF(OR(J{i}="",I{i}="frozen"),"",RANK(J{i},$J${first}:$J${end},1))')
        text(ws, i, 12, p.get("visibility", ""), align=top)
        text(ws, i, 13, p.get("branch", ""))
        if (i - first) % 2:
            for j in range(1, 14):
                ws.cell(i, j).fill = zebra
    tot = end + 1
    text(ws, tot, 2, "TOTAL", font=bold)
    put(ws, tot, 4, f"=SUM(D{first}:D{end})", font=bold)
    put(ws, tot, 5, f"=SUM(E{first}:E{end})", font=bold)
    put(ws, tot, 6, f"=IF(D{tot}=0,0,E{tot}/D{tot})", font=bold, fmt="0%")
    text(ws, tot + 1, 2, "Rows on the matrix tab (must equal TOTAL)", font=hint)
    put(ws, tot + 1, 4, "=COUNTA(matrix!$Q:$Q)-1")
    base = tot + 4
    header(ws, base, ["Shelf", "Category", "", "Cards", "Learned"])
    for d, (cname, _) in schema.CATEGORIES.items():
        r = base + 1 + d
        put(ws, r, 1, d)
        text(ws, r, 2, cname)
        put(ws, r, 4, f"=COUNTIF(matrix!$K:$K,A{r})")
        put(ws, r, 5, f"=SUMIF(matrix!$K:$K,A{r},matrix!$A:$A)")
    lv = base + 13
    header(ws, lv, ["Level", "Meaning", "", "Cards", "Learned"])
    for level, meaning in schema.LEVELS.items():
        r = lv + level
        put(ws, r, 1, level)
        text(ws, r, 2, meaning)
        put(ws, r, 4, f"=COUNTIF(matrix!$M:$M,A{r})")
        put(ws, r, 5, f"=SUMIF(matrix!$M:$M,A{r},matrix!$A:$A)")
    ws.freeze_panes = "C5"

    # ---- sit ----------------------------------------------------------------
    ws = wb.create_sheet("sit")
    text(ws, 1, 1, "Sit: attempt first. Type your answer in the yellow cell; the reference appears only then.", font=Font(name=FONT, size=12, bold=True), align=top)
    text(ws, 2, 1, "Example row: Your answer 'merge on batch_key so reruns replace' → Reveal shows the reference → Got it 1. When it is 0, write the gap in one line.", font=hint)
    ws.merge_cells("A2:J2")
    header(ws, 4, ["Code", "Project", "Type", "Question", "Snippet", "Your answer (fill in)", "Reveal", "Match", "Got it (0/1)", "Gap (what I missed)", "Card ID"],
           [7, 24, 9, 52, 40, 40, 52, 9, 11, 40, 26])
    got = DataValidation(type="list", formula1='"0,1"', allow_blank=True)
    ws.add_data_validation(got)
    sit_keep = dict(keep["sit"])
    for i, c in enumerate(cards, start=5):
        kept = sit_keep.pop(c["id"], {})
        src_col = "G" if c["type"] == "output" else "E"
        text(ws, i, 1, c["code"], align=top)
        text(ws, i, 2, title_of[c["slug"]])
        text(ws, i, 3, c["type"], align=top)
        text(ws, i, 4, c.get("question", "") + ("  " + schema.CLOZE_RE.sub("[...]", c["cloze"]) if c.get("cloze") else ""))
        text(ws, i, 5, c.get("code_snippet", ""))
        text(ws, i, 6, kept.get("answer") or "", fill=input_fill)
        put(ws, i, 7, f'=IF(LEN(TRIM(F{i}))=0,"(answer first)",INDEX(matrix!${src_col}:${src_col},MATCH($K{i},matrix!$Q:$Q,0)))', align=wrap)
        put(ws, i, 8, f'=IF(LEN(TRIM(F{i}))=0,"",IF(TRIM(F{i})=TRIM(G{i}),"exact","compare"))' if c["type"] == "output" else "")
        put(ws, i, 9, kept.get("got") if kept.get("got") in (0, 1) else None, fill=input_fill)
        text(ws, i, 10, kept.get("gap") or "", fill=input_fill)
        text(ws, i, 11, c["id"], align=top)
    sit_last = n + 4
    if n:
        got.add(f"I5:I{sit_last}")
    text(ws, sit_last + 2, 8, "passed:", font=bold, align=top)
    put(ws, sit_last + 2, 9, f"=COUNTIF(I5:I{sit_last},1)", font=bold)
    text(ws, sit_last + 3, 8, "failed:", font=bold, align=top)
    put(ws, sit_last + 3, 9, f"=COUNTIF(I5:I{sit_last},0)", font=bold)
    ws.freeze_panes = "E5"
    ws.auto_filter.ref = f"A4:K{max(sit_last, 5)}"

    # ---- drill --------------------------------------------------------------
    ws = wb.create_sheet("drill")
    text(ws, 1, 1, "project (blank = all)", font=bold, align=top)
    text(ws, 1, 2, keep["drill"].get("project") or "", fill=input_fill)
    text(ws, 2, 1, "highest level (1-4)", font=bold, align=top)
    level_keep = keep["drill"].get("level")
    put(ws, 2, 2, level_keep if isinstance(level_keep, (int, float)) and 1 <= level_keep <= 4 else 4, fill=input_fill)
    if projects:
        pick_list = DataValidation(type="list", formula1=f"=overview!$B${first}:$B${end}", allow_blank=True)
        ws.add_data_validation(pick_list)
        pick_list.add("B1")
    text(ws, 3, 1, "Press F9 (recalculate) for a new card. Say the answer aloud BEFORE reading column E.", font=hint)
    ws.merge_cells("A3:E3")
    header(ws, 5, ["row", "Code", "Question", "Hint: the anchor", "answer aloud BEFORE reading this column"], [22, 9, 60, 44, 60])
    pick = 'IFERROR(SMALL(matrix!$R:$R,RANDBETWEEN(1,COUNT(matrix!$R:$R))),"(fill matrix tab)")'
    for r in (6, 9, 10, 11):
        put(ws, r, 1, "=" + pick)
        for col, src in ((2, "B"), (3, "D"), (4, "F"), (5, "E")):
            put(ws, r, col, f'=IFERROR(INDEX(matrix!${src}:${src},$A{r}),"")', align=wrap)
    text(ws, 8, 1, "a mixed round of three", font=bold, align=top)
    text(ws, 13, 1, "project of the day", font=bold, align=top)
    put(ws, 13, 3, f'=IFERROR(INDEX(overview!$B${first}:$B${end},MATCH(1,overview!$K${first}:$K${end},0)),"")')
    text(ws, 14, 3, "the project whose estimated retention is lowest (overview, Priority 1)", font=hint)

    # ---- traces -------------------------------------------------------------
    ws = wb.create_sheet("traces")
    text(ws, 1, 1, "Ordered chains, one step per row: read the context, type the next step, then compare.", font=Font(name=FONT, size=12, bold=True), align=top)
    text(ws, 2, 1, "Example row: Context '[Beginning]' → Your next step 'discover new CSV exports' → Reveal shows the real step. □ marks the last step of a chain.", font=hint)
    ws.merge_cells("A2:G2")
    header(ws, 4, ["Code", "Project", "Sequence", "Step", "Context (two steps back)", "Your next step (fill in)", "Reveal", "Line", "Card ID"],
           [7, 24, 30, 6, 60, 44, 60, 4, 26])
    r = 5
    trace_keep = dict(keep["traces"])
    for c in cards:
        steps = c.get("steps") or []
        if c.get("type") != "trace" or not steps:
            continue
        for k, step in enumerate(steps, start=1):
            context = " / ".join(steps[max(0, k - 3):k - 1]) if k > 1 else "[Beginning]"
            line = step + (" □" if k == len(steps) else "")
            text(ws, r, 1, c["code"], align=top)
            text(ws, r, 2, title_of[c["slug"]])
            text(ws, r, 3, c.get("concept", ""))
            put(ws, r, 4, k)
            text(ws, r, 5, context)
            text(ws, r, 6, trace_keep.pop(f"{c['id']}#{k}", "") or "", fill=input_fill)
            put(ws, r, 7, f'=IF(LEN(TRIM(F{r}))=0,"(answer first)",H{r})', align=wrap)
            text(ws, r, 8, line)
            text(ws, r, 9, c["id"], align=top)
            r += 1
    trace_rows = r - 5
    ws.column_dimensions["H"].hidden = True  # the step itself: shown through Reveal once an attempt is typed
    ws.freeze_panes = "E5"

    # ---- taxonomy -----------------------------------------------------------
    ws = wb.create_sheet("taxonomy")
    text(ws, 1, 1, "The code IS the mnemonic: digit 1 = portfolio shelf, digit 2 = project slot, digit 3 = knowledge shelf, digit 4 = rank (0 = most fundamental).",
         font=Font(name=FONT, size=12, bold=True), align=top)
    header(ws, 3, ["Digit", "Knowledge shelf (third digit)", "Digit mnemonic"], [8, 38, 70])
    for d, (cname, mnemonic) in schema.CATEGORIES.items():
        put(ws, 4 + d, 1, d)
        text(ws, 4 + d, 2, cname)
        text(ws, 4 + d, 3, mnemonic)
    header(ws, 16, ["Digit", "Portfolio shelf (first digit)", "Digit mnemonic"])
    for d, (dname, mnemonic) in address.DOMAINS.items():
        put(ws, 17 + d, 1, d)
        text(ws, 17 + d, 2, dname)
        text(ws, 17 + d, 3, mnemonic)
    grid = 29
    text(ws, grid, 1, "Cards per project and knowledge shelf (row = project, column = third digit)", font=bold, align=top)
    header(ws, grid + 1, ["Code", "Project"] + [f"_{d}_" for d in range(10)])
    for i, p in enumerate(projects, start=grid + 2):
        text(ws, i, 1, p["code"], align=top)
        text(ws, i, 2, title_of[p["slug"]])
        for d in range(10):
            put(ws, i, 3 + d, f"=COUNTIFS(matrix!$C:$C,$B{i},matrix!$K:$K,{d})")
    for d in range(10):
        ws.column_dimensions[get_column_letter(3 + d)].width = 70 if d == 0 else 6
    ws.column_dimensions["C"].width = 70

    # ---- index --------------------------------------------------------------
    ws = wb.create_sheet("index")
    text(ws, 1, 1, "my index — fill from your attempt (allowed in the exam)", font=Font(name=FONT, size=12, bold=True), align=top)
    text(ws, 2, 1, "Fill these after a practice sit, before the next one. Example: trap I hit → 'counts doubled · merge ran outside its transaction · wrap delete+insert in one'.", font=hint)
    ws.merge_cells("A2:G2")
    header(ws, 4, ["Code", "Project"] + INDEX_SLOTS + ["Slug"], [7, 30, 44, 40, 40, 40, 60, 24])
    index_keep = dict(keep["index"])
    for i, p in enumerate(projects, start=5):
        kept = index_keep.pop(p["slug"], [None] * 5)
        text(ws, i, 1, p["code"], align=top)
        text(ws, i, 2, title_of[p["slug"]])
        for j in range(5):
            text(ws, i, 3 + j, kept[j] or "", fill=input_fill)
        text(ws, i, 8, p["slug"], align=top)
    ws.freeze_panes = "C5"

    # ---- notes that lost their card -----------------------------------------
    lost = [(cid, v) for cid, v in orphans.items()] + [(cid, v) for cid, v in sit_keep.items()] + [(k, v) for k, v in trace_keep.items()]
    if lost:
        ws = wb.create_sheet("orphans")
        header(ws, 1, ["Card ID no longer in the edition", "What you had typed there"], [34, 120])
        for i, (cid, value) in enumerate(lost, start=2):
            text(ws, i, 1, cid)
            text(ws, i, 2, value if isinstance(value, str) else "; ".join(f"{k}: {v}" for k, v in value.items() if v not in (None, "")))

    wb.save(out_path)
    verify_workbook(out_path, edition, title_of, trace_rows)
    return out_path


def verify_workbook(path: str, edition: dict, title_of: dict, trace_rows: int) -> dict:
    """Reload the file and prove the sheet agrees with the edition; raise on any mismatch."""
    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb["matrix"]
    cards = edition["cards"]
    rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if isinstance(r[16], str) and ":" in r[16]]
    problems = []
    if len(rows) != len(cards):
        problems.append(f"matrix has {len(rows)} card rows, the edition has {len(cards)}")
    by_id = {c["id"]: c for c in cards}
    per_project: dict[str, int] = {}
    for r in rows:
        card = by_id.get(r[16])
        if not card:
            problems.append(f"matrix row for unknown card id {r[16]}")
            continue
        if r[1] != card["code"]:
            problems.append(f"{r[16]}: code {r[1]!r} differs from {card['code']!r}")
        if r[11] not in schema.CARD_TYPES or r[12] not in schema.LEVELS or r[10] not in schema.CATEGORIES:
            problems.append(f"{r[16]}: type/level/category outside the controlled vocabulary")
        if r[0] not in (0, 1):
            problems.append(f"{r[16]}: Learned must be 0 or 1")
        per_project[r[2]] = per_project.get(r[2], 0) + 1
    expected = {}
    for c in cards:
        expected[title_of[c["slug"]]] = expected.get(title_of[c["slug"]], 0) + 1
    if per_project != expected:
        problems.append("cards per project on the sheet differ from the edition")
    for name in ("instructions", "overview", "matrix", "sit", "drill", "traces", "taxonomy", "index"):
        if name not in wb.sheetnames:
            problems.append(f"tab {name!r} is missing")
    if wb["traces"].max_row - 4 < trace_rows:
        problems.append("traces tab is shorter than the number of steps")
    if problems:
        raise ValueError("workbook verification failed: " + "; ".join(problems[:6]))
    return {"cards": len(rows), "projects": len(per_project), "trace_steps": trace_rows, "learned": sum(1 for r in rows if r[0] == 1)}


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
