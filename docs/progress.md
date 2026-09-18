# Progress Log

## 2026-08-19 — Project skeleton
- Folder structure created (data/raw_reports/{5 banks}, sql/, src/, docs/, notebooks/)
- requirements.txt drafted (pdfplumber, camelot-py[cv], psycopg2, sqlalchemy, pandas, python-dotenv)
- .env.example + .gitignore in place
- src/config.py: bank list + DB URL builder
- src/extraction.py, src/load_to_db.py, src/ratios.py: stubbed with
  docstrings/planned signatures — NOT implemented yet
- sql/schema.sql: draft table list only, not finalized
- README.md: full scope, architecture, ratios, and constraints written up

## 2026-08-20 — PDFs in hand (2022-2026, all 5 banks); extraction started
- Confirmed: user has all 5 banks x 2022-2026 annual reports already downloaded.
- Inspected ICICI_2022.pdf in detail (321 pages, text layer present, clean
  extraction, no OCR needed):
    - Standalone Balance Sheet / P&L / Cash Flow: pages 155-157
    - Consolidated Balance Sheet / P&L / Cash Flow: pages 257-259
    - Schedules 1-16 (line-item detail behind BS/P&L): pages 158-167
    - CRAR: disclosed directly in a clean table (Schedule note, p.179)
    - Gross/Net NPA: clean table in MD&A (p.139), in absolute Rs, not %
    - NIM, Net NPA ratio: shown ONLY as infographic charts (p.6) --
      NOT reliably machine-extractable. Decision: compute NIM ourselves
      from Schedule 13/15 (Interest Earned/Expended) + avg earning assets,
      rather than trying to parse the chart.
    - CASA is not a single line item -- Schedule 3 (Deposits) splits into
      Demand deposits (from banks / from others), Savings deposits, Term
      deposits (from banks / from others). CASA = Savings + Demand-from-others.
      This is a judgment call, documented in code, not a lookup.
  - Decision: use STANDALONE statements for cross-bank comparison (not
    consolidated), since regulatory ratios like CRAR are bank-only figures.

- Implemented src/extraction.py page-locator (locate_statements()):
    - Searches every page for statement/schedule header lines rather than
      hardcoding page numbers, since page numbers shift bank-to-bank and
      year-to-year.
    - Two title-line filters: strict (must be fully uppercase in source --
      for BS/P&L/CF headers, where prose false-positives are the real risk)
      vs. loose (for SCHEDULE N - ... headers, which can carry lowercase
      parentheticals like "[net of provisions]").
    - Bug found + fixed: initial version matched "balance sheet" inside a
      merged multi-column MD&A sentence (pdfplumber joins side-by-side
      columns into one text line on some pages) -- fixed by requiring the
      matched line to already be uppercase in the source PDF, not just
      after we uppercase it for comparison.
    - Bug found + fixed: consolidated statements weren't detected --
      initial regex was anchored to `^BALANCE SHEET` etc. and so never
      matched "CONSOLIDATED BALANCE SHEET". Fixed with an optional
      `(CONSOLIDATED\s+)?` prefix group, which also cleanly signals
      standalone vs. consolidated via whether that group matched.
    - Validated against ICICI_2022.pdf: all 3 standalone statements, all
      3 consolidated statements, and all 16 schedules located correctly
      on first full pass after fixes.

## Next up
- [ ] Build the actual table-parsing step: turn a located page into a
      clean DataFrame (pdfplumber extract_table / camelot).
- [ ] Design sql/schema.sql now that real line items are confirmed across
      2 banks (private + PSU).
- [ ] Implement src/load_to_db.py.
- [ ] Implement ratio calculations (likely as SQL views).
- [ ] Build Power BI dashboard.
- [ ] Once table-parsing works for ICICI + SBI + HDFC + Kotak, test Axis
      too before assuming full generalization -- 4 of 5 banks down, but
      Axis could still introduce its own quirks (each bank so far has).
- [ ] For Kotak specifically: real table-parsing will need coordinate-
      based extraction (crop by x-position to split left/right columns)
      rather than plain extract_text()/extract_table(), since its
      two-column page layout interleaves BS and P&L content in reading
      order. This is a bigger lift than the other 3 banks and should be
      scoped explicitly, not discovered mid-implementation.

## 2026-08-20 (cont'd) — Scope decision: defer Kotak, sequence the build
- Discussed dropping Kotak entirely given its column-split table-parsing
  need is real added scope on top of an already-working locator.
- Decision: KEEP Kotak in the 5-bank set, but SEQUENCE the work rather
  than blocking on it. Plan:
    1. Build the full pipeline (table-parsing -> PostgreSQL load -> ratio
       calculation -> Power BI dashboard) end-to-end using the 4 banks
       whose statements sit on ordinary single-statement-per-page layouts
       (ICICI, SBI, HDFC confirmed; Axis not yet tested, expected similar
       to this group since nothing about it suggests a Kotak-style
       multi-column layout).
    2. Once that full pipeline works and is demoable on its own, circle
       back and add Kotak-specific handling: coordinate/bounding-box based
       column-splitting so its BS/P&L/CF pages can be parsed the same way
       as the other banks' single-statement pages once split.
  Rationale: the locator work already done for Kotak (page-finding) is
  the hard, exploratory part and it's solved; what's left (crop left vs.
  right column by x-position, then reuse the same table-parsing logic
  per column) is a bounded, well-understood technique, not an open
  unknown. Sequencing this way means the project has a complete, working
  4-bank pipeline as a fallback at all times, and the Kotak extension
  becomes a documented "handled a genuinely different report layout"
  addition rather than a blocking dependency for seeing anything work.

## 2026-08-20 (cont'd) — SBI stress test, header matching generalized
- Uploaded SBI_2022.pdf (300 pages, private-bank-style text layer, clean
  extraction). Ran the ICICI-tuned locator against it as-is: schedules
  1-16 all located correctly (same "SCHEDULE N - ..." convention as
  ICICI), but Balance Sheet / P&L / Cash Flow all came back NOT FOUND.
- Root cause: SBI titles statements completely differently from ICICI --
  Title Case with the date folded into the same line, not bare all-caps
  with the date on the next line:
    ICICI:  "BALANCE SHEET"                          (bare caps, own line)
            "at March 31, 2022"                       (date, separate line)
    SBI:    "Balance Sheet as at 31st March, 2022"    (Title Case, one line)
  Same pattern held for P&L ("Profit and Loss Account for the year ended
  ...") and Cash Flow ("Cash Flow Statement for the year ended ..."), and
  for their Consolidated equivalents (Title Case "Consolidated " prefix).
- Fix: extraction.py now tries both header conventions per statement type:
    - "bare" pattern: exact all-caps line match (ICICI style) -- still
      requires the line to already be uppercase in the source, to guard
      against the original false-positive bug (a multi-column MD&A
      sentence that happened to start with lowercase "balance sheet").
    - "dated" pattern: Title Case + a distinctive date-suffix phrase
      ("as at"/"as on"/"for the year ended") (SBI style) -- matched
      case-insensitively; safe on its own because prose is very unlikely
      to reproduce that exact phrase structure at a line's start.
  Re-ran against both PDFs after the fix: SBI now resolves BS/P&L/CF at
  pages 118/127/185 (standalone) and 198/207/244 (consolidated); ICICI
  regression-tested clean, no change to its previously-correct output.
- Spot-checked SBI's Schedule 3 (Deposits) line items against ICICI's --
  identical structure (Demand Deposits from banks/others, Savings Bank
  Deposits, Term Deposits from banks/others), so the CASA-derivation
  logic planned earlier needs no bank-specific changes for SBI.

## 2026-08-20 (cont'd) — HDFC stress test, third header style + real typo found
- Uploaded HDFC_2022.pdf (426 pages, clean text layer). Ran the
  ICICI+SBI-tuned locator against it as-is: everything came back NOT
  FOUND, including all 16 schedules -- the biggest miss so far.
- Root cause #1 (statements): HDFC uses a THIRD header convention, distinct
  from both prior banks -- Title Case, bare, date on the next line:
    ICICI:  "BALANCE SHEET"                        (bare, ALL CAPS)
            "at March 31, 2022"
    SBI:    "Balance Sheet as at 31st March, 2022"  (dated, Title Case, one line)
    HDFC:   "Balance Sheet"                          (bare, Title Case)
            "As at March 31, 2022"
  A bare Title Case match is riskier than ICICI's bare ALL CAPS match --
  found an actual false-positive candidate: an "International Business"
  infographic elsewhere in the report captioned "Balance Sheet" as a
  standalone line with no date following. Fixed by requiring bare Title
  Case matches to additionally confirm the very next non-blank line looks
  like a date ("As at ..." / "As on ..." / "For the year ended ...");
  the infographic case is filtered out because its next line is ordinary
  prose, not a date.
- Root cause #2 (schedules): none of HDFC's 16 schedules were found even
  though the "Schedule N - ..." convention looked identical to ICICI/SBI
  at a glance. Turned out to be two separate issues:
    (a) HDFC also renders schedule headers in Title Case ("Schedule 1 -
        Capital") rather than all caps -- same fix pattern as the
        statement headers, made SCHEDULE_HEADER_RE case-insensitive.
    (b) A genuine typo in HDFC's own source PDF: page 221 reads "Shedule
        3 - Deposits" (missing the "c"). This is not a code bug or a
        pdfplumber extraction artifact -- confirmed by checking the raw
        page text directly. Handled explicitly by tolerating an optional
        "c" in "Schedule"/"Shedule", documented in code as a real,
        observed typo rather than a general fuzzy-match fallback (which
        would risk silently swallowing other kinds of mismatches).
- Re-ran against all 3 PDFs after both fixes: HDFC now resolves cleanly
  (BS/P&L/CF at 217/218/219 standalone, 305/306/307 consolidated, all 16
  schedules in correct sequential order); ICICI and SBI regression-tested
  clean, no change to previously-correct output.
- Spot-checked HDFC's Schedule 3 (Deposits) -- identical line-item
  structure to ICICI/SBI (Demand/Savings/Term, split by banks vs.
  others), so CASA logic still needs no bank-specific changes.
- Running tally: 3 header styles, 1 schedule-numbering typo, found across
  3 of 5 banks. This is exactly the "genuine data-cleaning work, not a
  rubber-stamp read_pdf() call" the project scope called out up front --
  worth keeping in the resume narrative as concrete evidence, not just
  the general claim.

## 2026-08-20 (cont'd) — Kotak stress test, dual-column page layout
- Uploaded KOTAK_2022.pdf (196 pages). Ran the locator as it stood after
  the HDFC fixes: statement headers all NOT FOUND, and schedules came
  back with Schedule 3 missing and several schedule page numbers clearly
  wrong (mixing what turned out to be data from both the standalone and
  consolidated sections unpredictably).
- Root cause: Kotak's page layout is structurally different from all 3
  previous banks, not just differently worded:
    - Balance Sheet and Profit & Loss are printed as TWO COLUMNS ON THE
      SAME PAGE, not on separate sequential pages. pdfplumber's reading
      order concatenates both titles onto one line: "Consolidated
      Balance Sheet Consolidated Profit and Loss Account".
    - The Cash Flow Statement gets its own page, but its columns are a
      continuation of the SAME statement (not two different statements),
      so the title line reads "Cash Flow Statement Cash Flow Statement"
      (repeated, not two different names).
    - Schedule pages are also two-column: a schedule header can appear
      mid-line preceded by leftover numeric content merged in from the
      previous column ("...1,981,835,668) Equity SCHEDULE 3 - DEPOSITS"),
      or two schedule headers can land on the same line ("SCHEDULE 3 -
      DEPOSITS SCHEDULE 7 - BALANCES WITH BANKS..."). This is why
      Schedule 3 specifically went missing: our previous schedule regex
      was anchored to line-start with a length cap, and every occurrence
      of "SCHEDULE 3" in Kotak's PDF fails at least one of those
      constraints.
    - Kotak also orders CONSOLIDATED financial statements BEFORE
      standalone (opposite of ICICI/SBI/HDFC, which all put standalone
      first) -- confirmed via the schedules-section markers "Forming
      part of Consolidated Balance Sheet..." (p.58ish) vs "Forming Part
      of Balance Sheet..." (p.143ish, no "Consolidated").
- Fixes:
    1. New multi-token line matcher (_match_multi_token_line): matches a
       line that decomposes ENTIRELY into one or more recognized
       statement-name tokens (each optionally "Consolidated"-prefixed)
       with nothing left over, confirmed by the next line looking like a
       date -- same safety mechanism as HDFC's single-token bare Title
       Case style, generalized to handle 1+ tokens instead of exactly 1.
       This one function now covers both HDFC's and Kotak's "bare Title
       Case" conventions.
    2. Schedule regex changed from anchored-at-line-start with a length
       cap to an unanchored `finditer` search with a lookahead that stops
       each match's name capture at the next "Schedule N" token or end of
       line -- handles both the "preceded by merged leftover content" and
       "two schedules on one line" cases without a hard length limit.
- Re-ran against all 4 PDFs (Kotak + prior 3) after both fixes: Kotak now
  resolves cleanly -- standalone BS+P&L both on p.141, standalone CF on
  p.142, consolidated BS+P&L both on p.56, consolidated CF on p.57, all
  16 schedules on p.58-61 (consolidated) in correct order. ICICI, SBI,
  HDFC regression-tested clean.
- Known remaining gap, not fixed (not needed for our ratios): Schedule
  17/18 lookups are now unreliable for some banks (e.g. ICICI Schedule 18
  resolved to page 62, clearly wrong) because the unanchored schedule
  search also matches inline prose references like "Refer Note 3 -
  Schedule 17" that appear earlier in the document than the real header.
  Since Schedules 1-16 (which cover everything our ratios need -- CASA,
  NIM inputs, CRAR-adjacent disclosures, NPA) are unaffected and resolve
  correctly across all 4 banks tested, this is a documented known
  limitation rather than a blocker.

## 2026-08-21 — Axis tested, 4-bank locator set complete
- Uploaded AXIS_2022.pdf (typical InDesign-produced report, clean text
  layer). Ran the locator as-is (no changes going in) since the plan was
  to skip Axis too if it turned out as structurally different as Kotak.
- Result: passed cleanly on the FIRST run, no fixes needed. All 3
  standalone statements (BS/P&L/CF at pages 174/175/176), all 3
  consolidated statements (262/263/264), and all 16 schedules (178-184,
  correct sequential order) resolved correctly.
- Axis uses the same "bare Title Case, date on next line" convention as
  HDFC (Style C) -- already handled, no new header style needed. It also
  prints an explicit "Standalone" / "Consolidated" label directly above
  the statement title, an even cleaner signal than HDFC provides, though
  not currently relied upon (the existing "Consolidated " prefix check
  on the title line itself already works and stays consistent with how
  the other banks are handled).
- Spot-checked Schedule 3 (Deposits) -- identical structure to the other
  3 banks (Demand/Savings/Term, split by banks vs. others). CASA logic
  needs no changes for Axis.
- Decision: since Axis needed zero new work, it's included in the "4
  easier banks" group for the table-parsing pipeline (ICICI, SBI, HDFC,
  Axis), with Kotak deferred per the earlier sequencing decision.

## Next up
- [ ] Build the actual table-parsing step: turn a located page into a
      clean DataFrame (pdfplumber extract_table / camelot), for the 4
      single-column-layout banks (ICICI, SBI, HDFC, Axis).
- [ ] Design sql/schema.sql now that real line items are confirmed across
      4 banks.
- [ ] Implement src/load_to_db.py.
- [ ] Implement ratio calculations (likely as SQL views).
- [ ] Build Power BI dashboard.
- [ ] Circle back for Kotak's column-split handling once the 4-bank
      pipeline works end-to-end.

## 2026-08-21 (cont'd) — Table parsing built and validated on all 4 banks
- Implemented parse_statement_page() in extraction.py: turns a located
  statement page into a DataFrame of (section, label, schedule_ref,
  current_year_value, prior_year_value) rows.
- Line shape (consistent across ICICI/SBI/HDFC/Axis via pdfplumber's
  default text extraction): "<Label> [<schedule#>] <value1> <value2>".
  Schedule refs are always 1-18 with no comma, distinguishing them from
  amount values (which always carry a comma at 4+ digits in Indian
  numbering). Values use standard accounting negative notation
  (parentheses) and "-" for nil, both handled by _parse_amount().
- Two false-positive row types found and fixed along the way (both are
  the statement page's own date/column headers being mistaken for data
  rows, since they share the shallow "<text> <num> <num>" shape our
  regex looks for):
    1. The statement's own single date line ("at March 31, 2022" /
       "As at 31st March, 2022") -- fixed with an explicit skip for
       lines starting with "at/as at/as on/for the year ended" + a
       month name.
    2. The column-header row spelling out both years side by side
       ("March 31, 2022 March 31, 2021", seen in HDFC) -- a different
       shape from #1 (no "as at" prefix), so needed its own check.
       ICICI's equivalent column header happens to use a DD.MM.YYYY
       format that our value-token regex naturally rejects (two decimal
       points breaks the single-decimal assumption), so it wasn't
       initially caught by symptom -- but that was a coincidence, not a
       real fix, so added the explicit month-name-based check rather
       than relying on it holding for other banks/years.
- Validated against all 4 banks' Balance Sheets: 15-16 rows each, all
  values correct (cross-checked several against the raw page text by
  hand). Axis has one extra line item ("Employees' Stock Options
  Outstanding") that SBI/HDFC don't carry -- a genuine cross-bank
  difference, not a parsing bug.
- Validated against ICICI's Cash Flow Statement (34 rows) specifically
  for the harder cases: negative values in parentheses and nil values as
  bare "-", both parsed correctly (e.g. "(22,143,504)" -> -22143504.0,
  "-" -> NaN).
- Validated against ICICI's P&L: Interest earned (sched 13), Interest
  expended (sched 15), Operating expenses (sched 16) all captured
  correctly -- these are exactly the inputs NIM and Cost-to-Income need.
- Known limitation, not yet fixed: sub-section headings written in mixed
  case rather than ALL CAPS (e.g. "Adjustments for:", "Cash flow
  from/(used in) investing activities") aren't recognized as section
  markers by the current all-caps-only section-header check, so they get
  folded into the next line item's label instead of being tracked
  separately. Values are unaffected -- this is label-cleanliness noise
  only. Low priority since downstream ratio lookups will likely key off
  known canonical labels (e.g. "advances", "deposits") rather than exact
  string match anyway.
- Known limitation, not yet fixed: parser assumes a statement fits on
  one page. True for all 4 banks tested so far; not yet handling the
  case where a statement genuinely overflows onto a second page.

## Next up
- [ ] Extract and validate Schedule 3 (Deposits, for CASA) across all 4
      banks the same way -- schedule pages have a slightly different
      shape (some have sub-item lettering/roman numerals like "A. I.
      Demand Deposits (i) From banks") that the current parser hasn't
      been tested against yet.
- [ ] Design sql/schema.sql now that parsed output shape is confirmed.
- [ ] Implement src/load_to_db.py.
- [ ] Implement ratio calculations (likely as SQL views).
- [ ] Build Power BI dashboard.
- [ ] Circle back for Kotak's column-split handling once the 4-bank
      pipeline works end-to-end.

## 2026-08-22 — Schedule parsing (Schedule 3 / Deposits, for CASA)
- Ran the statement-page parser as-is against ICICI's Schedule 3 page:
  mostly correct values, but content bled across into Schedule 4
  (Borrowings) because a bare "SCHEDULE 4 - BORROWINGS" header line
  doesn't match any of parse_statement_page's line-item/section-
  header/skip patterns, so it silently got folded in as a label
  continuation instead of stopping the page there. Root issue:
  parse_statement_page was designed for single-statement pages
  (BS/P&L/CF); schedule pages routinely hold multiple schedules back to
  back (ICICI's Schedule 3 and 4 share one page).
- Fix: new parse_schedule() function, bounded to the lines between this
  schedule's own header and the next schedule's header on the same page
  (found via SCHEDULE_HEADER_RE.finditer across every line, same
  mechanism the locator uses). Reuses the same line-item/value parsing
  as parse_statement_page underneath.
- Validated against Schedule 3 (Deposits) on all 4 banks -- all values
  correct, clean cutoff before the next schedule's content in every case.
- Genuine finding, not a bug: the label "(ii) From others" appears TWICE
  per bank's Deposits schedule -- once under "Demand deposits", once
  under "Term deposits" -- so it can't be resolved by label-text matching
  alone. Since every bank's schedule puts the Demand deposits section
  first (A. I. Demand deposits, then II. Savings, then III. Term), the
  reliable disambiguation is POSITIONAL: the first "From others" row in
  parse order is always Demand-from-others, which is exactly the CASA
  input needed. This will be the extraction strategy used in ratios.py's
  CASA calculation, documented here so the reasoning isn't lost by the
  time that code gets written.
- HDFC's Deposits schedule includes extra subtotal rows (a "Total" after
  the Demand i)/ii) pair, and another after the Term i)/ii) pair) that
  SBI/ICICI/Axis don't have. Genuine structural variance, not a parsing
  bug -- doesn't break the positional CASA strategy above, since the
  subtotal rows come AFTER the i)/ii) rows they summarize either way.

## Next up
- [ ] Design sql/schema.sql -- table-parsing output shape is now
      confirmed for Balance Sheet, P&L, Cash Flow, and schedules.
- [ ] Implement src/load_to_db.py.
- [ ] Implement ratio calculations (likely as SQL views), starting with
      the CASA positional-disambiguation logic noted above.
- [ ] Build Power BI dashboard.
- [ ] Circle back for Kotak's column-split handling once the 4-bank
      pipeline works end-to-end.
