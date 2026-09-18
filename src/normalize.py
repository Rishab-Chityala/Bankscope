"""
Normalizes raw_line_items into financial_facts: canonical (bank, year,
key) -> value, ready for ratio calculation.

Usage (from bankscope root):
    python -m src.normalize                    # normalize everything found
    python -m src.normalize icici_bank 2022     # normalize one bank/year

DESIGN: schedule_ref, not label text, is the primary matching key for
Balance Sheet / P&L line items. Schedule numbering in Indian bank annual
reports is RBI-mandated and identical across banks by regulation --
Schedule 1 is always Capital, Schedule 3 is always Deposits, Schedule 13
is always Interest Earned, regardless of how a given bank phrases the
label. This is far more reliable than label text, which we've already
found varies in wording, wraps across lines, and occasionally has
section headers merged into it (see the "Total" row handling below).
Label matching is only used as a fallback for line items with no
schedule reference (Total rows, Net profit).
"""
import sys
import re

import pandas as pd
from sqlalchemy import create_engine, text

from src.config import SQLALCHEMY_DATABASE_URL

BALANCE_SHEET_SCHEDULE_MAP = {
    1: "capital",
    2: "reserves_and_surplus",
    3: "total_deposits",
    8: "total_investments",
    9: "total_advances",
}
PROFIT_LOSS_SCHEDULE_MAP = {
    13: "interest_earned",
    14: "other_income",
    15: "interest_expended",
    16: "operating_expenses",
}

# Matches every observed spelling of the Balance Sheet's two grand-total
# rows: ICICI's "TOTAL CAPITAL AND LIABILITIES"/"TOTAL ASSETS" (self-
# descriptive), SBI's bare "TOTAL", HDFC's "Total", Axis's "Total".
_TOTAL_ROW_RE = re.compile(
    r"^TOTAL(\s+(CAPITAL AND LIABILITIES|ASSETS))?$", re.IGNORECASE
)


def get_engine():
    return create_engine(SQLALCHEMY_DATABASE_URL)


def _resolve_total_rows(bs: pd.DataFrame) -> list[tuple[str, float, int]]:
    """
    Disambiguate the Balance Sheet's two "Total" rows into
    total_liabilities and total_assets.

    Deliberately does NOT rely on the 'section' column: confirmed against
    real data that Axis's section field is empty for these rows, because
    its "ASSETS" section header got merged into the following line
    item's label during extraction (a pdfplumber reading-order artifact,
    same root cause class as several earlier bugs -- see
    docs/progress.md) rather than staying on its own line. Position is
    used instead, which is safe because every bank examined so far lists
    the Capital and Liabilities section before the Assets section, in
    that order, with no exceptions found -- so the FIRST Total-like row
    in document order is always the liabilities total, the SECOND is
    always the assets total.

    Where a label is self-descriptive (ICICI says "TOTAL ASSETS"
    outright), that's used as a cross-check against the positional
    assignment rather than trusted blindly -- if it ever disagreed, that
    would mean the document-order assumption broke for some bank/year,
    and this prints a loud warning rather than silently mis-mapping.
    """
    total_rows = bs[bs["label"].str.strip().apply(lambda s: bool(_TOTAL_ROW_RE.match(s)))]

    if len(total_rows) != 2:
        print(
            f"    WARNING: expected exactly 2 Total-like rows on the Balance "
            f"Sheet, found {len(total_rows)}. Skipping total_assets/"
            f"total_liabilities for this bank/year -- needs manual review."
        )
        return []

    ordered = total_rows.sort_values("id")
    liab_row, assets_row = ordered.iloc[0], ordered.iloc[1]

    # Cross-check against self-descriptive labels where present.
    liab_label = liab_row["label"].strip().upper()
    assets_label = assets_row["label"].strip().upper()
    if "ASSETS" in liab_label or "LIABILITIES" in assets_label:
        print(
            f"    WARNING: Total row order looks reversed based on label "
            f"text ('{liab_row['label']}' then '{assets_row['label']}') -- "
            f"the document-order assumption may not hold here. Review "
            f"before trusting total_assets/total_liabilities."
        )

    return [
        ("total_liabilities", liab_row["current_year_value"], liab_row["id"]),
        ("total_assets", assets_row["current_year_value"], assets_row["id"]),
    ]


def normalize_bank_year(engine, bank_code: str, fiscal_year: int) -> int:
    """
    Normalize one (bank, year)'s raw_line_items into financial_facts.
    Delete-then-insert per (bank_code, fiscal_year).
    """
    with engine.connect() as conn:
        raw = pd.read_sql(
            text(
                """
                SELECT id, statement_type, section, schedule_ref, label, current_year_value
                FROM raw_line_items
                WHERE bank_code = :bank_code AND fiscal_year = :fiscal_year
                  AND consolidated = FALSE
                """
            ),
            conn,
            params={"bank_code": bank_code, "fiscal_year": fiscal_year},
        )

    facts: list[tuple[str, float, int]] = []

    bs = raw[raw["statement_type"] == "balance_sheet"]
    pl = raw[raw["statement_type"] == "profit_loss"]
    sched3 = raw[raw["statement_type"] == "schedule_3"]

    for sched_ref, key in BALANCE_SHEET_SCHEDULE_MAP.items():
        match = bs[bs["schedule_ref"] == sched_ref]
        if not match.empty:
            row = match.iloc[0]
            facts.append((key, row["current_year_value"], row["id"]))

    facts.extend(_resolve_total_rows(bs))

    for sched_ref, key in PROFIT_LOSS_SCHEDULE_MAP.items():
        match = pl[pl["schedule_ref"] == sched_ref]
        if not match.empty:
            row = match.iloc[0]
            facts.append((key, row["current_year_value"], row["id"]))

    # Flexible Net Profit matching: captures "(loss)", "period", and "year" variations
    net_profit_rows = pl[
        pl["label"].str.lower().str.contains("net profit", na=False)
        & (
            pl["label"].str.lower().str.contains("for the year", na=False)
            | pl["label"].str.lower().str.contains("for the period", na=False)
        )
        & ~pl["label"].str.lower().str.contains("brought forward", na=False)
    ]

    if net_profit_rows.empty:
        # Fallback regex for fragmented line wraps
        net_profit_rows = pl[
            pl["label"].str.lower().str.contains(r"net\s+profit", regex=True, na=False)
        ]

    if not net_profit_rows.empty:
        # Take iloc[0] to guarantee a single Net Profit entry
        row = net_profit_rows.iloc[0]
        facts.append(("net_profit", row["current_year_value"], row["id"]))

    # Schedule 3 (Deposits): CASA components
    from_others_rows = sched3[sched3["label"].str.lower().str.contains("from others", na=False)]
    if not from_others_rows.empty:
        row = from_others_rows.sort_values("id").iloc[0]
        facts.append(("demand_deposits_from_others", row["current_year_value"], row["id"]))

    savings_rows = sched3[sched3["label"].str.lower().str.contains("savings", na=False)]
    if not savings_rows.empty:
        row = savings_rows.iloc[0]
        facts.append(("savings_deposits", row["current_year_value"], row["id"]))

    # Enforce strict uniqueness in memory on canonical_key prior to database entry
    unique_facts = {}
    for key, val, src_id in facts:
        if key not in unique_facts:
            unique_facts[key] = (val, src_id)

    deduped_facts = [(k, v[0], v[1]) for k, v in unique_facts.items()]

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM financial_facts WHERE bank_code = :bank_code AND fiscal_year = :fiscal_year"),
            {"bank_code": bank_code, "fiscal_year": fiscal_year},
        )
        for canonical_key, value, source_id in deduped_facts:
            conn.execute(
                text(
                    """
                    INSERT INTO financial_facts (bank_code, fiscal_year, canonical_key, value, source_line_item_id)
                    VALUES (:bank_code, :fiscal_year, :canonical_key, :value, :source_id)
                    """
                ),
                {
                    "bank_code": bank_code,
                    "fiscal_year": fiscal_year,
                    "canonical_key": canonical_key,
                    "value": float(value) if value is not None else None,
                    "source_id": int(source_id),
                },
            )

    return len(deduped_facts)


def normalize_all(engine=None):
    engine = engine or get_engine()
    with engine.connect() as conn:
        pairs = pd.read_sql(
            text("SELECT DISTINCT bank_code, fiscal_year FROM raw_line_items ORDER BY bank_code, fiscal_year"),
            conn,
        )

    if pairs.empty:
        print("No data found in raw_line_items -- run load_to_db.py first.")
        return

    print(f"Normalizing {len(pairs)} bank/year combinations...\n")
    for _, row in pairs.iterrows():
        bank_code, fiscal_year = row["bank_code"], int(row["fiscal_year"])
        try:
            count = normalize_bank_year(engine, bank_code, fiscal_year)
            print(f"{bank_code:<20} {fiscal_year}  {count} facts")
        except Exception as e:
            print(f"{bank_code:<20} {fiscal_year}  FAILED: {e!r}")


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        engine = get_engine()
        count = normalize_bank_year(engine, sys.argv[1], int(sys.argv[2]))
        print(f"{count} facts written for {sys.argv[1]} {sys.argv[2]}")
    else:
        normalize_all()