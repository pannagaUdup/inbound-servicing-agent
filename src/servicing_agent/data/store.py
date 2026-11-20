"""In-memory indexes over the generated CSVs. Backs the mock LMS / payment feed (T2).

T2 can swap this for a Postgres loader without changing the tool signatures.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

_FLOAT = {"sanction_amount", "principal_outstanding", "emi_amount", "interest_rate", "overdue_amount",
          "bounce_charges", "penal_charges", "foreclosure_charge_pct", "amount"}
_INT = {"tenure_months", "tenure_left", "dpd", "bureau_score"}


def dpd_band(dpd: int | None) -> str:
    """Days past due in 10-day bands: 0-10, 10-20, 20-30, ... (the waiver ladder reads this)."""
    lo = max(0, int(dpd or 0)) // 10 * 10
    return f"{lo}-{lo + 10}"


def _read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k, v in r.items():
            if k in _FLOAT and v != "":
                r[k] = float(v)
            elif k in _INT and v != "":
                r[k] = int(v)
            elif k == "is_active":
                r[k] = v == "True"
        if "dpd" in r and not r.get("dpd_band"):
            r["dpd_band"] = dpd_band(r["dpd"] if r["dpd"] != "" else 0)
    return rows


class DataStore:
    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir)
        if not (data_dir / "accounts.csv").exists():
            raise FileNotFoundError(f"No generated data in {data_dir}. Run `make data` first.")
        self.customers = {r["customer_id"]: r for r in _read(data_dir / "customers.csv")}
        self.accounts = {r["account_id"]: r for r in _read(data_dir / "accounts.csv")}
        self.contacts = {r["contact_id"]: r for r in _read(data_dir / "contacts.csv")}
        self.payments = _read(data_dir / "payments.csv")
        self.waiver_history = _read(data_dir / "waiver_history.csv")

        self.links_by_account: dict[str, list[dict]] = defaultdict(list)
        for link in _read(data_dir / "account_contacts.csv"):
            self.links_by_account[link["account_id"]].append(link)
        self.payments_by_utr = {p["utr"]: p for p in self.payments}
        self.payments_by_account: dict[str, list[dict]] = defaultdict(list)
        for p in self.payments:
            if p["account_id"]:
                self.payments_by_account[p["account_id"]].append(p)
        self.waivers_by_account: dict[str, list[dict]] = defaultdict(list)
        for w in self.waiver_history:
            self.waivers_by_account[w["account_id"]].append(w)


def _update_account(self, account_id: str, **fields) -> dict | None:
    acc = self.accounts.get(account_id)
    if acc is not None:
        acc.update(fields)
    return acc


DataStore.update_account = _update_account
DataStore.persistent = False          # CSV base: paid-link deltas are replayed onto it at startup


@lru_cache(maxsize=4)
def load_store(data_dir: str) -> DataStore:
    return DataStore(Path(data_dir))
