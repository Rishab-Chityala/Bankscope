"""
Loads extracted line items into PostgreSQL's raw_line_items table.

Usage (from bankscope root):
    python -m src.load_to_db                                   # load every PDF found
    python -m src.load_to_db data/raw_reports/icici_bank/ICICI_2022.pdf   # load one file

Re-run behavior: DELETE-then-INSERT per (bank_code, fiscal_year), wrapped
in one transaction per file so a failed load can't leave a partial state
(the delete and the insert either both succeed or neither does). This
was a deliberate choice over append-only or skip-if-exists, since
extraction.py has already gone through several rounds of bug fixes and
will likely go through more -- re-running a fixed loader against
already-loaded data should always produce a clean, current result, not
accumulate stale rows from a buggy earlier run.
"""
import re
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from src.config import SQLALCHEMY_DATABASE_URL, RAW_REPORTS_DIR
from src.extraction import locate_statements, parse_statement_page, parse_schedule, ReportMap

# Schedules we actually load, beyond the 3 main statements. Scoped to
# exactly what the planned ratios need (see sql/schema.sql's
# financial_facts comment and src/ratios.py) rather than all ~16
# schedules -- Schedule 3 (Deposits) is needed for CASA now; more will be
# added here as ratio coverage grows (e.g. Schedule 9/Advances), each one
# a deliberate addition, not a blanket "load everything" default.
SCHEDULES_TO_LOAD = [3]

RAW_LINE_ITEM_COLUMNS = [
    "bank_code", "fiscal_year", "statement_type", "consolidated",
    "section", "schedule_ref", "label", "current_year_value",
    "prior_year_value", "source_page",
]


def get_engine():
    return create_engine(SQLALCHEMY_DATABASE_URL)


def _extract_fiscal_year(pdf_path: Path) -> int:
    """
    Pull a 4-digit year out of the filename. Files are named like
    'ICICI_2022.pdf' or 'SBI_2025.pdf' -- not a fixed prefix, so this
    searches for a 20xx pattern rather than assuming a specific format,
    since the exact naming (BANK_YEAR vs just YEAR) has already varied
    once between the original plan and what actually got downloaded.
    """
    match = re.search(r"(20\d{2})", pdf_path.stem)
    if not match:
        raise ValueError(f"Could not find a 4-digit year in filename: {pdf_path.name}")
    return int(match.group(1))


def _statement_frames(pdf_path: Path, rmap: ReportMap) -> list[tuple[str, bool, "pd.DataFrame"]]:
    """
    Returns a list of (statement_type, consolidated, DataFrame) for every
    statement the locator found on this PDF. Statements the locator
    couldn't find (a known, documented gap for some bank/year
    combinations -- see docs/progress.md) are silently skipped here
    rather than raising, so one missing statement doesn't block loading
    everything else that WAS found.
    """
    frames = []
    statement_specs = [
        ("balance_sheet", False, rmap.balance_sheet),
        ("profit_loss", False, rmap.profit_loss),
        ("cash_flow", False, rmap.cash_flow),
        ("balance_sheet", True, rmap.consolidated_balance_sheet),
        ("profit_loss", True, rmap.consolidated_profit_loss),
        ("cash_flow", True, rmap.consolidated_cash_flow),
    ]
    for stmt_type, consolidated, location in statement_specs:
        if location is None:
            continue
        df = parse_statement_page(pdf_path, location.page)
        df["source_page"] = location.page
        frames.append((stmt_type, consolidated, df))

    for sched_num in SCHEDULES_TO_LOAD:
        if sched_num not in rmap.schedules:
            continue
        df = parse_schedule(pdf_path, sched_num, rmap.schedules)
        df["source_page"] = rmap.schedules[sched_num]
        frames.append((f"schedule_{sched_num}", False, df))

    return frames


def load_pdf(engine, bank_code: str, pdf_path: Path) -> dict:
    """
    Load a single PDF into raw_line_items. Returns a summary dict for
    reporting: {'fiscal_year', 'rows_loaded', 'statements_found',
    'statements_missing'}.
    """
    fiscal_year = _extract_fiscal_year(pdf_path)
    rmap = locate_statements(pdf_path)
    frames = _statement_frames(pdf_path, rmap)

    all_rows = []
    statements_found = []
    for stmt_type, consolidated, df in frames:
        if df.empty:
            continue
        df = df.copy()
        df["bank_code"] = bank_code
        df["fiscal_year"] = fiscal_year
        df["statement_type"] = stmt_type
        df["consolidated"] = consolidated
        # parse_schedule()'s output has no 'section'/'schedule_ref' columns
        # (those are specific to parse_statement_page's BS/P&L/CF shape);
        # add them as NULL so every frame has the same column set before
        # concatenating and loading.
        for col in ("section", "schedule_ref"):
            if col not in df.columns:
                df[col] = None
        all_rows.append(df[RAW_LINE_ITEM_COLUMNS])
        statements_found.append(f"{stmt_type}{'(consolidated)' if consolidated else ''}")

    valid_rows = [df for df in all_rows if not df.empty and not df.isna().all().all()]

    # Replace your current concatenation logic around line 128 with this:
    if valid_rows:
        combined = pd.concat(valid_rows, ignore_index=True)
        # Cast explicitly to resolve pandas dtype ambiguity on empty/all-NA columns
        combined = combined.astype({
            'schedule_ref': 'Int64',  # Nullable integer type
            'current_year_value': 'float64',
            'prior_year_value': 'float64'
        }, errors='ignore')
    else:
        combined = pd.DataFrame(columns=RAW_LINE_ITEM_COLUMNS)

    expected = ["balance_sheet", "profit_loss", "cash_flow"]
    missing = [s for s in expected if s not in [f.replace("(consolidated)", "") for f in statements_found]]

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM raw_line_items WHERE bank_code = :bank_code AND fiscal_year = :fiscal_year"),
            {"bank_code": bank_code, "fiscal_year": fiscal_year},
        )
        if not combined.empty:
            combined.to_sql("raw_line_items", conn, if_exists="append", index=False)

    return {
        "fiscal_year": fiscal_year,
        "rows_loaded": len(combined),
        "statements_found": statements_found,
        "statements_missing": missing,
    }


def load_all(engine=None):
    """Load every PDF under data/raw_reports/{bank}/*.pdf."""
    import time

    engine = engine or get_engine()

    if not RAW_REPORTS_DIR.exists():
        print(f"No such directory: {RAW_REPORTS_DIR}")
        return

    all_files = []
    for bank_dir in sorted(RAW_REPORTS_DIR.iterdir()):
        if not bank_dir.is_dir():
            continue
        for pdf_path in sorted(bank_dir.glob("*.pdf")):
            all_files.append((bank_dir.name, pdf_path))

    if not all_files:
        print("No PDFs found under data/raw_reports/*/*.pdf")
        return

    # Loading a large PDF genuinely takes 45-90+ seconds (locate_statements
    # scans every page once, and each statement/schedule parse re-opens
    # the PDF) -- printing progress live, as each file finishes, is what
    # distinguishes "still working" from "stuck" here, same lesson learned
    # from test_locator_batch.py's earlier silent-batch bug.
    print(f"Loading {len(all_files)} PDFs into raw_line_items.")
    print("Each file can take 45-90+ seconds for a large report -- that's expected.\n")

    batch_start = time.time()
    for i, (bank_code, pdf_path) in enumerate(all_files, start=1):
        file_start = time.time()
        try:
            result = load_pdf(engine, bank_code, pdf_path)
            elapsed = time.time() - file_start
            status = f"{result['rows_loaded']} rows"
            if result["statements_missing"]:
                status += f"  (missing: {result['statements_missing']})"
            print(f"[{i}/{len(all_files)}] {elapsed:>5.1f}s  {bank_code:<20} {pdf_path.name:<20} {status}")
        except Exception as e:
            elapsed = time.time() - file_start
            print(f"[{i}/{len(all_files)}] {elapsed:>5.1f}s  {bank_code:<20} {pdf_path.name:<20} FAILED: {e!r}")

    print(f"\nTotal time: {time.time() - batch_start:.0f}s")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        single_path = Path(sys.argv[1])
        if not single_path.exists():
            print(f"No such file: {single_path}")
            sys.exit(1)
        bank_code_arg = single_path.parent.name
        engine = get_engine()
        result = load_pdf(engine, bank_code_arg, single_path)
        print(result)
    else:
        load_all()
