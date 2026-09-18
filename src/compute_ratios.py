"""
Computes financial ratios from financial_facts and writes to computed_ratios.

Target metrics:
- casa_ratio (%)
- cost_to_income_ratio (%)
- roa (%)
- roe (%)
- credit_deposit_ratio (%)
- investment_deposit_ratio (%)
- net_interest_margin (%)
"""
import sys
import pandas as pd
from sqlalchemy import create_engine, text
from src.config import SQLALCHEMY_DATABASE_URL

def get_engine():
    return create_engine(SQLALCHEMY_DATABASE_URL)

def compute_ratios_for_bank_year(engine, bank_code: str, fiscal_year: int) -> int:
    """
    Fetch financial facts for a given bank and fiscal year, compute ratios,
    and insert/overwrite records in computed_ratios.
    """
    with engine.connect() as conn:
        df = pd.read_sql(
            text("""
                SELECT canonical_key, value 
                FROM financial_facts 
                WHERE bank_code = :bank_code AND fiscal_year = :fiscal_year
            """),
            conn,
            params={"bank_code": bank_code, "fiscal_year": fiscal_year}
        )

    if df.empty:
        print(f"No financial facts found for {bank_code} {fiscal_year}. Skipping.")
        return 0

    facts = dict(zip(df['canonical_key'], df['value']))

    ratios = {}

    # Helper function for safe division
    def safe_div(num, denom, multiplier=100.0):
        if num is not None and denom is not None and denom != 0:
            return (float(num) / float(denom)) * multiplier
        return None

    # 1. CASA Ratio (%)
    demand = facts.get('demand_deposits_from_others', 0) or 0
    savings = facts.get('savings_deposits', 0) or 0
    total_dep = facts.get('total_deposits')
    ratios['casa_ratio'] = safe_div(demand + savings, total_dep)

    # 2. Cost-to-Income Ratio (%)
    op_exp = facts.get('operating_expenses')
    int_earned = facts.get('interest_earned', 0) or 0
    int_expended = facts.get('interest_expended', 0) or 0
    oth_income = facts.get('other_income', 0) or 0
    net_interest_income = int_earned - int_expended
    total_income = net_interest_income + oth_income
    ratios['cost_to_income_ratio'] = safe_div(op_exp, total_income)

    # 3. Return on Assets - ROA (%)
    net_profit = facts.get('net_profit')
    total_assets = facts.get('total_assets')
    ratios['roa'] = safe_div(net_profit, total_assets)

    # 4. Return on Equity - ROE (%)
    capital = facts.get('capital', 0) or 0
    reserves = facts.get('reserves_and_surplus', 0) or 0
    equity = capital + reserves
    ratios['roe'] = safe_div(net_profit, equity)

    # 5. Credit-to-Deposit Ratio (%)
    advances = facts.get('total_advances')
    ratios['credit_deposit_ratio'] = safe_div(advances, total_dep)

    # 6. Investment-to-Deposit Ratio (%)
    investments = facts.get('total_investments')
    ratios['investment_deposit_ratio'] = safe_div(investments, total_dep)

    # 7. Net Interest Margin Proxy (%)
    ratios['net_interest_margin'] = safe_div(net_interest_income, total_assets)

    # Delete-then-insert execution pattern
    # Delete-then-insert execution pattern
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM computed_ratios WHERE bank_code = :bank_code AND fiscal_year = :fiscal_year"),
            {"bank_code": bank_code, "fiscal_year": fiscal_year}
        )

        for ratio_key, value in ratios.items():
            if value is not None:
                conn.execute(
                    text("""
                        INSERT INTO computed_ratios (bank_code, fiscal_year, ratio_name, computed_value)
                        VALUES (:bank_code, :fiscal_year, :ratio_key, :value)
                    """),
                    {
                        "bank_code": bank_code,
                        "fiscal_year": fiscal_year,
                        "ratio_key": ratio_key,
                        "value": round(value, 4)
                    }
                )

    return len([v for v in ratios.values() if v is not None])

def compute_all_ratios():
    engine = get_engine()
    with engine.connect() as conn:
        pairs = pd.read_sql(
            text("SELECT DISTINCT bank_code, fiscal_year FROM financial_facts ORDER BY bank_code, fiscal_year"),
            conn
        )

    if pairs.empty:
        print("No facts found in financial_facts -- run src.normalize first.")
        return

    print(f"Computing ratios across {len(pairs)} bank/year combinations...\n")
    for _, row in pairs.iterrows():
        bank_code, fiscal_year = row['bank_code'], int(row['fiscal_year'])
        count = compute_ratios_for_bank_year(engine, bank_code, fiscal_year)
        print(f"{bank_code:<20} {fiscal_year}  {count} ratios computed")

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        engine = get_engine()
        c = compute_ratios_for_bank_year(engine, sys.argv[1], int(sys.argv[2]))
        print(f"{c} ratios computed for {sys.argv[1]} {sys.argv[2]}")
    else:
        compute_all_ratios()