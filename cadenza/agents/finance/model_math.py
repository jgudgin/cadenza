"""Pure calculation - no LLM, no I/O, nothing async. The three statements
follow from the assumptions by arithmetic alone, and they are built to
balance by construction (net working capital is carried on the balance
sheet, not just netted into cash flow): if `validate` ever reports an
imbalance, that is a real bug in this module, not something a model
"sometimes" does. The dynamic, judgement-driven part of this workflow is
the assumptions themselves - whether they're complete and plausible - not
the arithmetic.
"""

from __future__ import annotations

PROJECTION_YEARS = 3

REQUIRED_ASSUMPTIONS: dict[str, tuple[float, float | None]] = {
    "starting_revenue": (0, None),
    "revenue_growth": (-0.9, 3.0),
    "gross_margin": (0, 1),
    "opex_pct_revenue": (0, 1),
    "da_pct_revenue": (0, 0.5),
    "capex_pct_revenue": (0, 0.5),
    "nwc_pct_revenue": (-0.5, 1),
    "tax_rate": (0, 0.6),
    "starting_cash": (0, None),
    "starting_debt": (0, None),
}


def project_income_statement(a: dict, years: int = PROJECTION_YEARS) -> dict[str, dict]:
    statements: dict[str, dict] = {}
    prev_revenue = a["starting_revenue"]
    for y in range(1, years + 1):
        revenue = prev_revenue * (1 + a["revenue_growth"])
        cogs = revenue * (1 - a["gross_margin"])
        gross_profit = revenue - cogs
        opex = revenue * a["opex_pct_revenue"]
        ebitda = gross_profit - opex
        da = revenue * a["da_pct_revenue"]
        ebit = ebitda - da
        tax = max(ebit, 0.0) * a["tax_rate"]
        net_income = ebit - tax
        statements[f"year_{y}"] = {
            "revenue": revenue,
            "cogs": cogs,
            "gross_profit": gross_profit,
            "opex": opex,
            "ebitda": ebitda,
            "da": da,
            "ebit": ebit,
            "tax": tax,
            "net_income": net_income,
        }
        prev_revenue = revenue
    return statements


def project_cash_flow(a: dict, income_statement: dict, years: int = PROJECTION_YEARS) -> dict[str, dict]:
    flows: dict[str, dict] = {}
    prev_cash = a["starting_cash"]
    prev_nwc = a["nwc_pct_revenue"] * a["starting_revenue"]
    for y in range(1, years + 1):
        key = f"year_{y}"
        line = income_statement[key]
        capex = line["revenue"] * a["capex_pct_revenue"]
        nwc = a["nwc_pct_revenue"] * line["revenue"]
        delta_nwc = nwc - prev_nwc
        cfo = line["net_income"] + line["da"] - delta_nwc
        cfi = -capex
        net_cash_flow = cfo + cfi
        ending_cash = prev_cash + net_cash_flow
        flows[key] = {
            "cfo": cfo,
            "capex": capex,
            "cfi": cfi,
            "delta_nwc": delta_nwc,
            "net_cash_flow": net_cash_flow,
            "beginning_cash": prev_cash,
            "ending_cash": ending_cash,
        }
        prev_cash = ending_cash
        prev_nwc = nwc
    return flows


def project_balance_sheet(
    a: dict, income_statement: dict, cash_flow: dict, years: int = PROJECTION_YEARS
) -> dict[str, dict]:
    sheets: dict[str, dict] = {}
    prev_ppe = 0.0
    starting_nwc = a["nwc_pct_revenue"] * a["starting_revenue"]
    # Day-1 equity is whatever balances the opening position - there is no
    # prior year to roll forward from, so it is derived rather than given.
    prev_equity = a["starting_cash"] + starting_nwc - a["starting_debt"]
    for y in range(1, years + 1):
        key = f"year_{y}"
        income = income_statement[key]
        flow = cash_flow[key]
        nwc = a["nwc_pct_revenue"] * income["revenue"]
        ppe = prev_ppe + flow["capex"] - income["da"]
        cash = flow["ending_cash"]
        total_assets = cash + nwc + ppe
        debt = a["starting_debt"]  # no financing activity modelled - documented simplification
        equity = prev_equity + income["net_income"]
        total_liab_and_equity = debt + equity
        sheets[key] = {
            "cash": cash,
            "nwc": nwc,
            "ppe": ppe,
            "total_assets": total_assets,
            "debt": debt,
            "equity": equity,
            "total_liab_and_equity": total_liab_and_equity,
            "imbalance": total_assets - total_liab_and_equity,
        }
        prev_ppe = ppe
        prev_equity = equity
    return sheets


def check_assumptions(a: dict) -> list[str]:
    """Shared by the agent that first drafts assumptions (to decide whether
    to ask again) and the agent that validates the finished model (defence
    in depth - re-check downstream rather than only trusting the earlier
    check passed)."""
    issues: list[str] = []
    for key, (lo, hi) in REQUIRED_ASSUMPTIONS.items():
        if key not in a:
            issues.append(f"missing assumption: {key}")
            continue
        value = a[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            issues.append(f"{key} is not numeric: {value!r}")
            continue
        if lo is not None and value < lo:
            issues.append(f"{key}={value} is below the plausible minimum {lo}")
        if hi is not None and value > hi:
            issues.append(f"{key}={value} is above the plausible maximum {hi}")
    return issues


def validate(a: dict, balance_sheet: dict) -> dict:
    """Two independent checks, both real: does the arithmetic balance
    (a correctness guarantee, checked every run, not just in tests), and
    are the assumptions themselves plausible (a data-quality judgement -
    this is the one that can genuinely fail, because it depends on what an
    LLM produced, not on this module's own arithmetic)."""
    issues = check_assumptions(a)
    max_imbalance = max((abs(v["imbalance"]) for v in balance_sheet.values()), default=0.0)
    return {
        "balances": max_imbalance < 1.0,  # a dollar of tolerance is float rounding, not a bug
        "max_imbalance": round(max_imbalance, 4),
        "assumption_issues": issues,
    }
