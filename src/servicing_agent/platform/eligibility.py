"""Automated waiver/settlement eligibility evaluation (owner: T2).

A deterministic policy check over ``config/policy_bands.yaml`` — it never decides the case, it
tells reviewers where it sits: the DPD band limits, the approval authority level for the amount,
whether it needs the credit-head tier, and whether it is within band. Shown to approvers as a
policy fact, exactly like the mock's per-case eligibility snapshot.
"""

from __future__ import annotations

from typing import Any


def dpd_bucket(dpd: int) -> str:
    if dpd <= 0:
        return "0"
    if dpd <= 30:
        return "1-30"
    if dpd <= 60:
        return "31-60"
    if dpd <= 90:
        return "61-90"
    return "90+"


def authority_level(amount: float, levels: list[dict]) -> str:
    for lv in levels:
        if lv.get("max_amount") is None or amount <= lv["max_amount"]:
            return lv["level"]
    return levels[-1]["level"] if levels else "L4_credit_committee"


# Authority levels at or above which the dedicated credit-head tier is required.
_CREDIT_HEAD_LEVELS = {"L3_regional_head", "L4_credit_committee"}


def evaluate(req: dict[str, Any], account: dict[str, Any] | None, bands_cfg: dict[str, Any]) -> dict[str, Any]:
    amount = req.get("amount") or 0
    comps = req.get("components") or []
    dpd = int((account or {}).get("dpd") or 0)
    bucket = dpd_bucket(dpd)
    band = (bands_cfg.get("bands", {}).get("default", {}) or {}).get(bucket, {}) or {}
    levels = bands_cfg.get("authority_levels", []) or []
    level = authority_level(amount, levels)

    reasons: list[str] = []
    within_band = True
    if "principal" in comps and (band.get("max_principal_waiver_pct", 0) or 0) == 0:
        within_band = False
        reasons.append(f"principal waiver not permitted at DPD {bucket}")
    if "interest" in comps:
        reasons.append("interest component needs credit-head review")

    requires_credit_head = (level in _CREDIT_HEAD_LEVELS) or ("principal" in comps) or ("interest" in comps)
    recommendation = "auto_ok" if (within_band and not requires_credit_head) else "needs_review"

    return {
        "dpd_bucket": bucket,
        "authority_level": level,
        "within_band": within_band,
        "requires_credit_head": requires_credit_head,
        "max_penal_waiver_pct": band.get("max_penal_waiver_pct"),
        "max_principal_waiver_pct": band.get("max_principal_waiver_pct"),
        "reasons": reasons,
        "recommendation": recommendation,
    }
