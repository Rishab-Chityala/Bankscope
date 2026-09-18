"""
Diagnostic tool: when the locator fails to find a statement in a PDF,
this searches every page for short lines mentioning "balance sheet" /
"profit and loss" / "cash flow" (case-insensitive) so we can see what
the REAL header text looks like there, instead of guessing.

"""
import sys
from pathlib import Path

import pdfplumber

TARGETS = ["balance sheet", "profit and loss", "profit & loss", "cash flow"]


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.diagnose_headers <path_to_pdf>")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"No such file: {pdf_path}")
        sys.exit(1)

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            for line in text.split("\n"):
                stripped = line.strip()
                if len(stripped) > 90:
                    continue  # skip long prose lines, we only want short header-like ones
                lower = stripped.lower()
                if any(t in lower for t in TARGETS):
                    print(f"page {i + 1}: {stripped!r}")


if __name__ == "__main__":
    main()