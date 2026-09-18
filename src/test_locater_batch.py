"""
Batch-test the statement locator across every PDF currently sitting in
data/raw_reports/{bank}/{year}.pdf, and print a clean pass/fail summary.

Run from the bankscope root:
    python -m src.test_locator_batch

Checks, per file:
    - All 3 standalone statements found (Balance Sheet, P&L, Cash Flow)
    - All 3 consolidated statements found
    - All of Schedules 3, 8, 9, 13, 14, 15, 16 found -- these are the only
      schedules our planned ratios (CASA, NIM, Cost-to-Income, etc.)
      actually depend on, per ratios.py. We don't check schedules 1-2,
      4-7, 10-12, 17-18 for "in order" correctness, since we've already
      confirmed (see docs/progress.md) that the unanchored schedule
      search can pick up false-positive prose references to schedules we
      don't use -- that's a known, accepted, non-blocking limitation, not
      something this script needs to re-flag every run.
    - The 7 required schedules appear in ascending page order relative to
      each other (a cheap sanity check: a real schedule sequence is
      always printed in order; an out-of-order page number is a strong
      signal of a false-positive prose match, same pattern already found
      for Schedule 11 on ICICI 2023).
"""
import sys
import time
from pathlib import Path

from src.extraction import locate_statements

REQUIRED_SCHEDULES = [3, 8, 9, 13, 14, 15, 16]

RAW_REPORTS_DIR = Path(__file__).resolve().parent.parent / "data" / "raw_reports"


def check_report(pdf_path: Path) -> list[str]:
    """Returns a list of problem descriptions; empty list means all clear."""
    problems = []
    rmap = locate_statements(pdf_path)

    for attr, label in [
        ("balance_sheet", "Standalone Balance Sheet"),
        ("profit_loss", "Standalone P&L"),
        ("cash_flow", "Standalone Cash Flow"),
        ("consolidated_balance_sheet", "Consolidated Balance Sheet"),
        ("consolidated_profit_loss", "Consolidated P&L"),
        ("consolidated_cash_flow", "Consolidated Cash Flow"),
    ]:
        if getattr(rmap, attr) is None:
            problems.append(f"{label} NOT FOUND")

    missing_scheds = [n for n in REQUIRED_SCHEDULES if n not in rmap.schedules]
    if missing_scheds:
        problems.append(f"Missing required schedules: {missing_scheds}")

    present = [(n, rmap.schedules[n]) for n in REQUIRED_SCHEDULES if n in rmap.schedules]
    pages_in_order = [p for _, p in present]
    if pages_in_order != sorted(pages_in_order):
        problems.append(
            f"Required schedules out of page order (likely false-positive "
            f"prose match): {present}"
        )

    return problems


def main():
    if not RAW_REPORTS_DIR.exists():
        print(f"No such directory: {RAW_REPORTS_DIR}")
        sys.exit(1)

    all_files = []
    for bank_dir in sorted(RAW_REPORTS_DIR.iterdir()):
        if not bank_dir.is_dir():
            continue
        for pdf_path in sorted(bank_dir.glob("*.pdf")):
            all_files.append((bank_dir.name, pdf_path))

    if not all_files:
        print("No PDFs found under data/raw_reports/*/*.pdf")
        sys.exit(0)

    # pdfplumber's text extraction is genuinely slow on large PDFs
    # (~30-60s for a 300+ page report is normal, not a hang) -- printing
    # each file's result AS IT FINISHES, with elapsed time, is what
    # actually distinguishes "still working" from "stuck" for the person
    # running this, since a batch of 20+ such files can take 15-20+
    # minutes with zero output otherwise.
    print(f"Found {len(all_files)} PDFs. Processing one at a time -- each can")
    print("take 30-60+ seconds for a large report; that's expected, not stuck.\n")
    print(f"{'Bank':<20} {'Year':<12} {'Time':<8} {'Status'}")
    print("-" * 70)

    results = []  # (bank, year, problems)
    batch_start = time.time()
    for i, (bank, pdf_path) in enumerate(all_files, start=1):
        year = pdf_path.stem
        file_start = time.time()
        try:
            problems = check_report(pdf_path)
        except Exception as e:
            problems = [f"CRASHED: {e!r}"]
        elapsed = time.time() - file_start
        results.append((bank, year, problems))

        status = "OK" if not problems else "ISSUES:"
        print(f"{bank:<20} {year:<12} {elapsed:>5.1f}s  [{i}/{len(all_files)}] {status}")
        for p in problems:
            print(f"    - {p}")

    total_elapsed = time.time() - batch_start
    pass_count = sum(1 for _, _, problems in results if not problems)

    print("-" * 70)
    print(f"{pass_count}/{len(results)} passed clean  (total time: {total_elapsed:.0f}s)")


if __name__ == "__main__":
    main()