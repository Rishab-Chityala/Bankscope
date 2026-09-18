"""
PDF table extraction layer.

STATUS: Page-location logic implemented and validated against all 5
banks' FY2022 annual reports (ICICI, SBI, HDFC, Kotak Mahindra, Axis).
Table-parsing (turning a located page into a clean DataFrame) is next.

Design note on why we search for header text rather than hardcoding page
numbers: page numbers shift bank to bank and year to year (a report can
gain or lose 10-20 pages between editions), so the pipeline must locate
statements by their printed section headers, not by position in the file.
"""
import re
from pathlib import Path
from dataclasses import dataclass, field
import pandas as pd

import pdfplumber


# Four distinct header conventions observed across banks so far:
#
#   Style A ("bare caps"): ICICI writes the statement title alone on its
#     own line, fully capitalized, with the date on a separate line below
#     ("BALANCE SHEET" / newline / "at March 31, 2022").
#
#   Style B ("dated Title Case"): SBI writes the title and date together
#     on one line, in Title Case ("Balance Sheet as at 31st March, 2022").
#     The "dated" patterns below require the date to be the END of the
#     line (not just checked as a prefix) -- found a real false positive
#     without this: Axis's Auditor's Report opinion paragraph contains
#     the sentence "Consolidated Balance Sheet as at March 31, 2022, the
#     Consolidated Profit and Loss Account and the Consolidated Cash Flow
#     Statement for the year then ended...", which starts with an exact
#     match of the dated pattern's prefix. Anchoring the date to line-end
#     rejects it, since a real header line has nothing after the date.
#
#   Style C ("bare Title Case, single token"): HDFC writes the title
#     alone on its own line like ICICI, but in Title Case rather than
#     all-caps, with the date on the next line ("Balance Sheet" /
#     newline / "As at March 31, 2022"). Because "Balance Sheet" alone
#     (2 words, Title Case) has real false-positive risk from
#     infographic captions elsewhere in the report (observed: an
#     "International Business" infographic with a standalone "Balance
#     Sheet" caption line), this style requires the very next non-blank
#     line to look like a date ("As at ..." / "As on ..." / "For the
#     year ended ...").
#
#   Style D ("bare Title Case, multi-token"): Kotak lays the Balance
#     Sheet and P&L out as two columns on the SAME page, and
#     pdfplumber's reading order concatenates both titles onto one line:
#     "Consolidated Balance Sheet Consolidated Profit and Loss Account".
#     Its Cash Flow Statement page does the analogous thing with the
#     same title repeated ("Cash Flow Statement Cash Flow Statement"),
#     since that single statement's columns just continue across the
#     page rather than holding two different statements. Handled by
#     _match_multi_token_line(), which requires the ENTIRE line to
#     decompose into one or more recognized statement-name tokens with
#     nothing left over, plus the same next-line date confirmation as
#     Style C -- both together make this about as safe as Style C
#     despite the looser per-token shape.
# Date suffix used to close off the "dated" statement-header patterns,
# covering both date orderings seen so far ("31st March, 2022" and
# "March 31, 2022"). Anchoring this to the END of the line (see below) is
# what makes the "dated" style safe against prose false positives -- see
# the Axis case documented next to STATEMENT_PATTERNS.
_DATE_SUFFIX = r"(?:\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+|[A-Za-z]+\s+\d{1,2})\s*,?\s*\d{4}"

STATEMENT_PATTERNS = {
    "balance_sheet": {
        "bare_upper": re.compile(r"^(?:STANDALONE\s+)?(CONSOLIDATED\s+)?BAL\s?ANCE SHEET$"),
        "dated": re.compile(
            rf"^(?:Standalone\s+)?(Consolidated\s+)?Bal\s?ance Sheet\s+(as at|as on)\s+{_DATE_SUFFIX}\.?$",
            re.IGNORECASE,
        ),
    },
    "profit_loss": {
        "bare_upper": re.compile(
            r"^(?:STANDALONE\s+)?(CONSOLIDATED\s+)?"
            r"(PROFIT AND LOSS ACCOUNT|PROFIT & LOSS ACCOUNT|STATEMENT OF PROFIT AND LOSS)$"
        ),
        "dated": re.compile(
            rf"^(?:Standalone\s+)?(Consolidated\s+)?"
            rf"(Profit and Loss Account|Profit & Loss Account|Statement of Profit and Loss)"
            rf"\s+for\s+the\s+year\s+ended\s+{_DATE_SUFFIX}\.?$",
            re.IGNORECASE,
        ),
    },
    "cash_flow": {
        "bare_upper": re.compile(r"^(?:STANDALONE\s+)?(CONSOLIDATED\s+)?CASH FLOW STATEMENT$"),
        "dated": re.compile(
            rf"^(?:Standalone\s+)?(Consolidated\s+)?Cash Flow Statement\s+for\s+the\s+year\s+ended\s+{_DATE_SUFFIX}\.?$",
            re.IGNORECASE,
        ),
    },
}
# NOTE on the "BAL\s?ANCE" (rather than plain "BALANCE") in the balance
# sheet patterns above: HDFC's FY2024 standalone Balance Sheet title
# extracts as 'STANDALONE BAL ANCE SHEET' -- a stray space inserted
# mid-word. Confirmed by dumping the raw, unfiltered page text directly
# (not guessed) -- this is a PDF font/kerning extraction artifact, not an
# editorial typo like HDFC's earlier "Shedule" (missing "c"). Different
# root cause, same lesson: source PDFs contain literal text imperfections
# the extraction layer has to tolerate explicitly, not assume away.
#
# NOTE on the "STANDALONE" prefix above: HDFC's FY2024 report (first
# full annual report after the July 2023 HDFC Bank / HDFC Ltd merger)
# switched from unprefixed bare-caps headers ("BALANCE SHEET") to
# explicitly prefixed ones ("STANDALONE BALANCE SHEET",
# "STANDALONE PROFIT AND LOSS ACCOUNT"). The prefix is deliberately
# non-capturing and simply discarded -- functionally identical to no
# prefix at all -- while "CONSOLIDATED" remains a capturing group used
# to set is_consolidated, matching the existing behavior for every other
# bank/year. Confirmed via direct inspection of HDFC_2024.pdf's page
# text (not guessed): 'STANDALONE PROFIT AND LOSS ACCOUNT' and
# 'STANDALONE CASH FLOW STATEMENT' both appear verbatim, all-caps.

# Known "chrome" text -- section labels, breadcrumbs, or running headers
# that pdfplumber's reading order sometimes glues onto the end of a
# statement's title line, because they sit at a similar page y-position
# and get merged during text extraction (same root cause as the
# ICICI/HDFC column-merge false-positives found earlier). Observed in
# HDFC_2023.pdf: the real title "Profit and Loss Account" appears as
# 'Profit and Loss Account Financial Statements' -- the trailing
# "Financial Statements" is a section-nav label, not part of the title,
# and is stripped before matching so it doesn't fail the "nothing left
# over" check in _match_multi_token_line. The Balance Sheet on the same
# report is unaffected ('Balance Sheet' appears clean) -- this chrome
# apparently isn't glued on for every statement's title, just some, so
# stripping it conditionally (only if present) is safe either way.
_TRAILING_CHROME_RE = re.compile(r"\s+Financial Statements$", re.IGNORECASE)

# One combined pattern used by both Style C (single match) and Style D
# (multiple matches) -- each match is one "[Consolidated ]<Statement
# Name>" token.
TITLECASE_TOKEN_RE = re.compile(
    r"(?:Standalone\s+)?(Consolidated\s+)?"
    r"(Bal\s?ance Sheet|Profit and Loss Account|Profit & Loss Account|"
    r"Statement of Profit and Loss|Cash Flow Statement)",
    re.IGNORECASE,
)

# Used to confirm a bare Title Case match (Style C or D) by checking the
# next non-blank line looks like a date, filtering out incidental short
# captions elsewhere in the report (infographics, pull-quotes) that
# happen to say just "Balance Sheet" with no date following.
DATE_LINE_RE = re.compile(r"^(As at|As on|For the year ended)\b", re.IGNORECASE)

# Case-insensitive: HDFC renders these as "Schedule 1 - capital" (Title
# Case) rather than ICICI/SBI's "SCHEDULE 1 - CAPITAL" (all caps).
# Also tolerates "Shedule" (missing the "c") -- a genuine typo found in
# HDFC's own FY2022 PDF ("Shedule 3 - Deposits", p.221). Documenting
# rather than silently correcting: source documents do contain outright
# typos, not just formatting variance, and the extraction layer has to
# plan for that rather than assume every report is internally consistent.
#
# Not anchored to line start and not globally length-limited: Kotak's
# two-column schedule pages produce lines like "SCHEDULE 3 - DEPOSITS
# SCHEDULE 7 - BALANCES WITH BANKS..." (two schedules on one line) and
# "1,984,661,760 (31st March, 2021: ...) Equity SCHEDULE 3 - DEPOSITS"
# (a schedule header preceded by leftover numeric content merged in from
# the adjacent column). The lookahead stops each match's name capture at
# the next "Schedule N" token or end of line, so multiple schedules
# sharing one merged line are still split correctly.
SCHEDULE_HEADER_RE = re.compile(
    r"S(?:c)?hedule\s+(\d+)\s*[-–]\s*(.+?)(?=\s+S(?:c)?hedule\s+\d|$)",
    re.IGNORECASE,
)


def _match_multi_token_line(lines: list[str], idx: int) -> list[tuple[str, bool]]:
    """
    Style C/D: match a line that consists ENTIRELY of one or more
    recognized statement-name tokens (each optionally "Consolidated "
    prefixed) with nothing else on the line, confirmed by a date-like
    next line. Covers both HDFC's single-token pages and Kotak's
    two-tokens-concatenated pages (BS+P&L sharing one page) and
    repeated-token pages (its Cash Flow Statement, whose columns
    continue rather than holding two different statements).
    """
    stripped = lines[idx].strip()
    stripped_for_match = _TRAILING_CHROME_RE.sub("", stripped)
    matches = list(TITLECASE_TOKEN_RE.finditer(stripped_for_match))
    if not matches:
        return []

    # Require the matched tokens to reconstruct the whole line (modulo
    # whitespace and the chrome suffix stripped above) -- i.e. nothing
    # left over. This is what makes a loose per-token pattern safe: real
    # prose won't consist ENTIRELY of one or more exact statement names
    # back to back.
    reconstructed = " ".join(m.group(0) for m in matches)
    if re.sub(r"\s+", " ", stripped_for_match) != re.sub(r"\s+", " ", reconstructed):
        return []

    date_confirmed = False
    for lookahead in lines[idx + 1 : idx + 3]:
        if not lookahead.strip():
            continue
        date_confirmed = bool(DATE_LINE_RE.match(lookahead.strip()))
        break
    if not date_confirmed:
        return []

    results = []
    for m in matches:
        name_lower = m.group(2).lower()
        if "balance sheet" in name_lower:
            stmt_type = "balance_sheet"
        elif "cash flow" in name_lower:
            stmt_type = "cash_flow"
        else:
            stmt_type = "profit_loss"
        results.append((stmt_type, bool(m.group(1))))
    return results


def _match_statement_header(lines: list[str], idx: int) -> tuple[str, bool] | None:
    """
    Try the bare-caps (Style A) and dated (Style B) header styles against
    lines[idx]. Styles C/D are handled separately by
    _match_multi_token_line, since they can yield more than one match
    per line. Returns (statement_type, is_consolidated) on the first
    match, or None.
    """
    stripped = lines[idx].strip()

    for stmt_type, patterns in STATEMENT_PATTERNS.items():
        upper_match = patterns["bare_upper"].match(stripped)
        if upper_match and stripped == stripped.upper():
            return stmt_type, bool(upper_match.group(1))

        dated_match = patterns["dated"].match(stripped)
        if dated_match:
            return stmt_type, bool(dated_match.group(1))

    return None


def _is_title_line(line: str) -> bool:
    """
    General length/punctuation guard applied before attempting any header
    match, to cheaply skip obvious prose lines.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 140:
        return False
    if stripped.endswith((".", ";", ":")):
        return False
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    return True


@dataclass
class StatementLocation:
    page: int  # 1-indexed, matches pdfplumber's pdf.pages[page - 1]
    consolidated: bool


@dataclass
class ReportMap:
    """All located sections for a single annual report PDF."""
    balance_sheet: StatementLocation | None = None
    profit_loss: StatementLocation | None = None
    cash_flow: StatementLocation | None = None
    consolidated_balance_sheet: StatementLocation | None = None
    consolidated_profit_loss: StatementLocation | None = None
    consolidated_cash_flow: StatementLocation | None = None
    schedules: dict[int, int] = field(default_factory=dict)  # schedule number -> page


def locate_statements(pdf_path: Path) -> ReportMap:
    """
    Two-pass scan of the PDF.

    Pass 1 locates the Balance Sheet / P&L / Cash Flow headers for both
    standalone and consolidated sections (page-order over the whole
    document -- these headers are rare enough, and specific enough, that
    scanning every page for them directly is safe).

    Pass 2 locates "SCHEDULE N - ..." headers, but ONLY within page
    ranges bounded by Pass 1's results (from each section's Cash Flow
    statement page onward, up to the start of the next section or end of
    document). This bound is necessary, not just tidy: Auditor's Reports
    routinely cite schedule numbers in prose before the actual financial
    statements begin (observed in Axis's FY2022 report: "Refer Schedule
    12 - Contingent Liabilities..." appears in the Key Audit Matters
    section, ~4 pages before the real Balance Sheet). An unbounded search
    picks up whichever occurrence comes first in the document, which
    without this fix would silently point Schedule 12 at the wrong page.
    """
    result = ReportMap()

    with pdfplumber.open(pdf_path) as pdf:
        page_texts: list[str] = []

        # --- Pass 1: statement headers ---
        for i, page in enumerate(pdf.pages):
            page_num = i + 1
            text = page.extract_text() or ""
            page_texts.append(text)  # reused in Pass 2, avoids re-extracting
            lines = text.split("\n")

            for idx, line in enumerate(lines):
                stripped = line.strip()
                if not stripped or not _is_title_line(stripped):
                    continue

                # Styles A/B (single match per line) first, then Styles
                # C/D (can yield multiple matches from one line, e.g.
                # Kotak's BS+P&L sharing a page).
                match = _match_statement_header(lines, idx)
                matches = [match] if match else _match_multi_token_line(lines, idx)

                for m in matches:
                    stmt_type, is_consolidated = m
                    loc = StatementLocation(page=page_num, consolidated=is_consolidated)
                    target_attr = (
                        f"consolidated_{stmt_type}" if is_consolidated else stmt_type
                    )
                    # keep first occurrence only
                    if getattr(result, target_attr) is None:
                        setattr(result, target_attr, loc)

        # --- Determine schedule search boundaries from Pass 1 results ---
        def _latest_page(*locs: StatementLocation | None) -> int | None:
            pages = [loc.page for loc in locs if loc is not None]
            return max(pages) if pages else None

        standalone_start = _latest_page(result.cash_flow, result.profit_loss, result.balance_sheet)
        consolidated_start = _latest_page(
            result.consolidated_cash_flow, result.consolidated_profit_loss, result.consolidated_balance_sheet
        )

        # Schedules live after whichever section's statements were found.
        # If a section wasn't found at all, don't search for its
        # schedules (nothing to bound the search against safely).
        search_ranges: list[tuple[int, int]] = []
        total_pages = len(pdf.pages)
        candidate_starts = sorted(p for p in (standalone_start, consolidated_start) if p is not None)
        for j, start in enumerate(candidate_starts):
            end = candidate_starts[j + 1] if j + 1 < len(candidate_starts) else total_pages
            search_ranges.append((start, end))

        # --- Pass 2: schedules, restricted to the ranges above ---
        for start, end in search_ranges:
            for page_num in range(start, end + 1):
                text = page_texts[page_num - 1]
                for line in text.split("\n"):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    for sched_match in SCHEDULE_HEADER_RE.finditer(stripped):
                        sched_num = int(sched_match.group(1))
                        result.schedules.setdefault(sched_num, page_num)

    return result


def print_report_map(report_map: ReportMap) -> None:
    """Human-readable summary, useful for manually sanity-checking a new bank/year."""
    print("Standalone statements:")
    print(f"  Balance Sheet:  page {report_map.balance_sheet.page if report_map.balance_sheet else 'NOT FOUND'}")
    print(f"  Profit & Loss:  page {report_map.profit_loss.page if report_map.profit_loss else 'NOT FOUND'}")
    print(f"  Cash Flow:      page {report_map.cash_flow.page if report_map.cash_flow else 'NOT FOUND'}")
    print("Consolidated statements:")
    print(f"  Balance Sheet:  page {report_map.consolidated_balance_sheet.page if report_map.consolidated_balance_sheet else 'NOT FOUND'}")
    print(f"  Profit & Loss:  page {report_map.consolidated_profit_loss.page if report_map.consolidated_profit_loss else 'NOT FOUND'}")
    print(f"  Cash Flow:      page {report_map.consolidated_cash_flow.page if report_map.consolidated_cash_flow else 'NOT FOUND'}")
    print(f"Schedules found: {sorted(report_map.schedules.keys())}")
    for num in sorted(report_map.schedules.keys()):
        print(f"  Schedule {num}: page {report_map.schedules[num]}")


# ---------------------------------------------------------------------------
# Table parsing: turn a located statement page into a clean DataFrame of
# (section, label, schedule_ref, current_year_value, prior_year_value) rows.
#
# Observed line shape (via pdfplumber's default, non-layout text extraction,
# consistent across ICICI/SBI/HDFC/Axis -- Kotak's two-column pages are
# handled separately, see module docstring):
#     "<Label text> [<schedule#>] <current_year_value> <prior_year_value>"
# e.g. "Capital 1 13,899,662 13,834,104"
#      "Employees stock options outstanding 2,664,141 31,010"   (no schedule#)
#      "Net (appreciation)/depreciation on investments 19,089,256 (22,143,504)"
#      "Employee Stock Options Expense 2,642,190 -"              (nil value)
# ---------------------------------------------------------------------------

# A value cell is either a signed/parenthesized number (parentheses = negative,
# the standard accounting convention seen throughout these reports) or a bare
# "-" meaning nil/not applicable.
_VALUE_TOKEN = r"(?:\(?-?[\d,]+\.?\d*\)?|-)"

# Schedule references are always 1-18 in these reports (matches the 16
# numbered schedules plus "17 & 18" for accounting policies/notes) and never
# carry a comma, unlike amount values -- that distinguishes a schedule
# reference from a genuinely small rupee amount.
LINE_ITEM_RE = re.compile(
    rf"^(?P<label>.+?)\s+(?:(?P<sched>1[0-8]|[1-9])\s+)?"
    rf"(?P<val1>{_VALUE_TOKEN})\s+(?P<val2>{_VALUE_TOKEN})$"
)

# Once any of these phrases appears, we've left the statement body and
# entered the auditor's signature block / boilerplate -- stop parsing the
# page at that point rather than risk absorbing signature-block text as
# label continuations.
END_OF_STATEMENT_MARKERS = [
    "schedules referred to above",
    "significant accounting policies",
    "as per our report",
    "for and on behalf of the board",
]

# The statement's own date header ("at March 31, 2022" / "As at 31st
# March, 2022" / "for the year ended March 31, 2022") has the same
# shallow shape as a real line item (short label text followed by two
# number-like tokens: day and year) and would otherwise be misparsed as
# a bogus row. Skipped explicitly before attempting LINE_ITEM_RE.
_DATE_HEADER_SKIP_RE = re.compile(
    r"^(at|as at|as on|for the year ended)\s+"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)",
    re.IGNORECASE,
)

# The column-header row spelling out both years side by side (e.g. "March
# 31, 2022 March 31, 2021") has a different shape from the single date
# line above (no "at"/"as at" prefix -- it starts directly with the month
# name) and would otherwise slip through as a bogus row, since it still
# superficially looks like "<label text> <num> <num>". Observed in HDFC;
# ICICI's equivalent column header uses a DD.MM.YYYY format that happens
# not to match LINE_ITEM_RE's value-token pattern (two decimal points
# breaks the single-decimal assumption), so it's naturally excluded there
# without needing this check -- but relying on that coincidence for every
# bank would be fragile, hence this explicit, format-agnostic check.
_MONTH_NAME_RE = (
    r"(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)"
)
_COLUMN_DATE_HEADER_RE = re.compile(
    rf"^(?:{_MONTH_NAME_RE}\s+\d{{1,2}},?\s+\d{{4}}\s*){{2}}$", re.IGNORECASE
)

# A short, fully-uppercase, digit-free line is almost always a section
# header ("CAPITAL AND LIABILITIES", "ASSETS") rather than a label
# continuation -- tracked as metadata and NOT folded into the next
# line item's label.
_SECTION_HEADER_RE = re.compile(r"^[A-Z][A-Z &/]{2,49}$")


def _parse_amount(token: str) -> float | None:
    
    """'-' -> None (nil/not applicable). '(1,234)' -> -1234.0. '1,234' -> 1234.0."""
    if not token:
        return None
        
    token = token.strip()
    if token == "-" or token == "" or token == "()":
        return None
        
    negative = token.startswith("(") and token.endswith(")")
    cleaned = token.strip("()").replace(",", "").strip()
    
    if not cleaned:
        return None
        
    try:
        value = float(cleaned)
        return -value if negative else value
    except ValueError:
        return None
    

def parse_statement_page(pdf_path: Path, page_num: int) -> "pd.DataFrame":
    """
    Parse a single located statement page (Balance Sheet, P&L, or Cash
    Flow) into a DataFrame with columns: section, label, schedule_ref,
    current_year_value, prior_year_value.

    KNOWN LIMITATION: assumes the statement fits on one page (true for
    all 4 banks checked so far: ICICI, SBI, HDFC, Axis). A label that
    wraps across two physical lines in the source (observed in ICICI's
    Cash Flow Statement, e.g. "Redemption/sale from/(investments in)
    subsidiaries and/or joint" wrapping onto the next line before the
    values appear) is stitched back together via the pending-label
    buffer below, but only within a single page -- a statement that
    genuinely overflows onto a second page is not yet handled and would
    silently truncate. Not hit yet in the 4 banks tested; flagged here
    so it's not a silent surprise later.
    """
    import pandas as pd  # local import keeps module importable without pandas for pure page-location use

    with pdfplumber.open(pdf_path) as pdf:
        text = pdf.pages[page_num - 1].extract_text() or ""

    rows = []
    pending_label = ""
    current_section = None
    found_first_row = False

    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue

        if any(marker in stripped.lower() for marker in END_OF_STATEMENT_MARKERS):
            break

        if _DATE_HEADER_SKIP_RE.match(stripped) or _COLUMN_DATE_HEADER_RE.match(stripped):
            continue

        match = LINE_ITEM_RE.match(stripped)
        if match:
            found_first_row = True
            label = (pending_label + " " + match.group("label")).strip()
            pending_label = ""
            rows.append(
                {
                    "section": current_section,
                    "label": label,
                    "schedule_ref": int(match.group("sched")) if match.group("sched") else None,
                    "current_year_value": _parse_amount(match.group("val1")),
                    "prior_year_value": _parse_amount(match.group("val2")),
                }
            )
            continue

        if _SECTION_HEADER_RE.match(stripped):
            current_section = stripped
            pending_label = ""
            continue

        # Not a line item, not a section header. Before the first real row
        # this is page furniture (report title, column headers like
        # "Schedule" / "At At") and safely ignored. After the first real
        # row it's most likely a wrapped label continuation.
        if found_first_row:
            pending_label = (pending_label + " " + stripped).strip()

    return pd.DataFrame(rows)


def parse_schedule(pdf_path: Path, schedule_num: int, schedules_map: dict[int, int]) -> "pd.DataFrame":
    """
    Parse a single schedule (e.g. Schedule 3 - Deposits, needed for CASA)
    into the same row shape as parse_statement_page: label,
    current_year_value, prior_year_value.

    Schedule pages often hold multiple schedules back to back (e.g.
    ICICI's Schedule 3 and Schedule 4 share page 160) -- parsing here is
    bounded to the lines between this schedule's own header and the next
    schedule's header (or end of page, if this is the last schedule on
    the page). Without that boundary, parse_statement_page's simpler
    "parse the whole page" approach bleeds the next schedule's content
    into this one, since a bare "SCHEDULE 4 - BORROWINGS" header line
    doesn't match any of the line-item/section-header/skip patterns and
    so gets silently folded in as a label continuation.

    KNOWN LIMITATION: assumes the target schedule's content fits within
    the same page as its header -- true for Schedule 3 (Deposits) on all
    4 banks checked, but a longer schedule (e.g. Investments, Advances)
    may span onto a second page, which this doesn't yet handle.
    """
    import pandas as pd

    page_num = schedules_map.get(schedule_num)
    if page_num is None:
        raise ValueError(f"Schedule {schedule_num} not found in schedules_map")

    with pdfplumber.open(pdf_path) as pdf:
        text = pdf.pages[page_num - 1].extract_text() or ""
    lines = text.split("\n")

    # Find every schedule-header occurrence on the page (a line can hold
    # more than one, e.g. Kotak's two-column layout), then bound this
    # schedule's content to [its own header, the next differing schedule's
    # header).
    header_lines = []  # (line_idx, schedule_number)
    for i, line in enumerate(lines):
        for m in SCHEDULE_HEADER_RE.finditer(line.strip()):
            header_lines.append((i, int(m.group(1))))

    start_idx = next((i for i, n in header_lines if n == schedule_num), None)
    if start_idx is None:
        raise ValueError(f"Schedule {schedule_num} header not found on page {page_num}")

    later_headers = [i for i, n in header_lines if i > start_idx and n != schedule_num]
    end_idx = min(later_headers) if later_headers else len(lines)

    rows = []
    pending_label = ""
    found_first_row = False

    for line in lines[start_idx:end_idx]:
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped.lower() for marker in END_OF_STATEMENT_MARKERS):
            break
        if _DATE_HEADER_SKIP_RE.match(stripped) or _COLUMN_DATE_HEADER_RE.match(stripped):
            continue
        if SCHEDULE_HEADER_RE.match(stripped):
            continue  # this schedule's own header line, already consumed

        match = LINE_ITEM_RE.match(stripped)
        if match:
            found_first_row = True
            label = (pending_label + " " + match.group("label")).strip()
            pending_label = ""
            rows.append(
                {
                    "label": label,
                    "current_year_value": _parse_amount(match.group("val1")),
                    "prior_year_value": _parse_amount(match.group("val2")),
                }
            )
            continue

        if _SECTION_HEADER_RE.match(stripped):
            pending_label = ""
            continue

        if found_first_row:
            pending_label = (pending_label + " " + stripped).strip()

    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not path or not path.exists():
        print("Usage: python -m src.extraction <path_to_pdf>")
        sys.exit(1)
    rmap = locate_statements(path)
    print_report_map(rmap)