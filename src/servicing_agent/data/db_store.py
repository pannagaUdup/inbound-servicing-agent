"""Loan data (D1 / D2 / D7) read from Postgres instead of the generated CSVs.

Same shape as ``DataStore`` (the mock LMS, payment feed and verification read the same dicts), so nothing else
changes. The tables are re-read when they're older than ``refresh_seconds`` (default 5), so an edit in Postgres
(say, a loan's dpd or pre-approved flag) is what the agent uses on the next message, without a restart.

    SA_DATA_SOURCE=db            # csv (default) | db
    SA_DATA_REFRESH_SECONDS=5

The tables are filled by ``make db-load`` from the CSVs; after that Postgres is the source the agent reads.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import create_engine, text

from servicing_agent.data.store import dpd_band

log = logging.getLogger(__name__)

_NUMERIC = {"sanction_amount", "principal_outstanding", "emi_amount", "interest_rate", "overdue_amount",
            "bounce_charges", "penal_charges", "foreclosure_charge_pct", "amount"}
_NAMES = ("customers", "accounts", "contacts", "payments", "waiver_history", "links_by_account",
          "payments_by_utr", "payments_by_account", "waivers_by_account")


def _clean(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal) or (k in _NUMERIC and v is not None):
            v = float(v)
        out[k] = "" if v is None and k in ("account_id", "settled_date", "payee_vpa") else v
    return out


class DbDataStore:
    def __init__(self, database_url: str, refresh_seconds: float = 5.0):
        self.engine = create_engine(database_url, pool_pre_ping=True)
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._data: dict = {}
        self._loaded_at = 0.0
        self._load()
        if not self._data["accounts"]:
            raise RuntimeError("Postgres has no accounts: run `make db-load` first, or set SA_DATA_SOURCE=csv")

    def _q(self, conn, sql: str) -> list[dict]:
        return [_clean(dict(r)) for r in conn.execute(text(sql)).mappings()]

    def _load(self) -> None:
        with self.engine.connect() as c:
            customers = {r["customer_id"]: r for r in self._q(c, "select * from customers")}
            accounts = {}
            for r in self._q(c, "select * from accounts"):
                r["dpd_band"] = r.get("dpd_band") or dpd_band(r.get("dpd"))
                accounts[r["account_id"]] = r
            contacts = {}
            for r in self._q(c, "select * from contacts"):
                r["is_active"] = bool(r.get("is_active"))
                contacts[r["contact_id"]] = r
            links = self._q(c, "select account_id, contact_id, role from account_contacts")
            payments = self._q(c, "select * from payment_transactions order by txn_date, txn_id")
            waivers = self._q(c, "select account_id, type, amount, components, decided_on, outcome from waiver_history")
        links_by_account: dict[str, list[dict]] = defaultdict(list)
        for link in links:
            links_by_account[link["account_id"]].append(link)
        payments_by_account: dict[str, list[dict]] = defaultdict(list)
        for p in payments:
            if p["account_id"]:
                payments_by_account[p["account_id"]].append(p)
        waivers_by_account: dict[str, list[dict]] = defaultdict(list)
        for w in waivers:
            waivers_by_account[w["account_id"]].append(w)
        self._data = {"customers": customers, "accounts": accounts, "contacts": contacts, "payments": payments,
                      "waiver_history": waivers, "links_by_account": links_by_account,
                      "payments_by_utr": {p["utr"]: p for p in payments},
                      "payments_by_account": payments_by_account, "waivers_by_account": waivers_by_account}
        self._loaded_at = time.monotonic()

    def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._loaded_at < self.refresh_seconds:
            return
        with self._lock:
            if not force and time.monotonic() - self._loaded_at < self.refresh_seconds:
                return
            try:
                self._load()
            except Exception:  # noqa: BLE001 - keep serving the last good copy if Postgres blips
                log.exception("loan data refresh from Postgres failed; using the previous copy")

    persistent = True                 # changes are written to Postgres, so nothing is replayed at startup

    def update_account(self, account_id: str, **fields) -> dict | None:
        """Write account fields (overdue, charges, dpd...) to Postgres, then re-read so the agent sees them."""
        cols = {k: v for k, v in fields.items() if k.isidentifier()}
        if cols:
            sets = ", ".join(f"{k} = :{k}" for k in cols)
            with self.engine.begin() as c:
                c.execute(text(f"update accounts set {sets} where account_id = :account_id"),
                          {**cols, "account_id": account_id})
            self.refresh(force=True)
        return self.accounts.get(account_id)

    def __getattr__(self, name: str):
        if name in _NAMES:
            self.refresh()
            return self._data[name]
        raise AttributeError(name)
