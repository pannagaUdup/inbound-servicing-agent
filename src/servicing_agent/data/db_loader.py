"""Load the synthetic D1/D2/D7 CSVs from data/generated into the database (owner: T3).

    python -m servicing_agent.data.db_loader                 # uses SA_DATABASE_URL
    python -m servicing_agent.data.db_loader --data data/generated --url postgresql+psycopg://...

Idempotent: each run replaces the contents of the source-system tables (customers,
accounts, contacts, account_contacts, payment_transactions, waiver_history) in one
transaction. Operational tables (cases, audit_events, tickets, media, ...) are untouched.

Mobile numbers are stored only as ``mobile_hash`` (same hash the campaign service uses),
never raw.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from sqlalchemy import delete, func, insert, select

from servicing_agent.config import get_settings
from servicing_agent.platform.campaigns import mobile_hash
from servicing_agent.platform.db import (
    Account, AccountContact, Contact, Customer, PaymentTransaction, WaiverHistory, make_session_factory,
)

_FLOAT = {"sanction_amount", "principal_outstanding", "emi_amount", "interest_rate", "overdue_amount",
          "bounce_charges", "penal_charges", "foreclosure_charge_pct", "amount"}
_INT = {"tenure_months", "tenure_left", "dpd", "bureau_score"}

# Insert order matters only for readability (no hard FKs); delete in reverse.
TABLES = [Customer, Account, Contact, AccountContact, PaymentTransaction, WaiverHistory]


def _read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k, v in list(r.items()):
            if v == "":
                r[k] = None
            elif k in _FLOAT:
                r[k] = float(v)
            elif k in _INT:
                r[k] = int(v)
            elif k == "is_active":
                r[k] = v == "True"
    return rows


def build_rows(data_dir: Path) -> dict[type, list[dict]]:
    contacts = []
    for c in _read(data_dir / "contacts.csv"):
        c["mobile_hash"] = mobile_hash(c.pop("mobile"))
        contacts.append(c)
    return {
        Customer: _read(data_dir / "customers.csv"),
        Account: _read(data_dir / "accounts.csv"),
        Contact: contacts,
        AccountContact: _read(data_dir / "account_contacts.csv"),
        PaymentTransaction: _read(data_dir / "payments.csv"),
        WaiverHistory: _read(data_dir / "waiver_history.csv"),
    }


def load(data_dir: Path, database_url: str, batch: int = 2000) -> dict[str, int]:
    rows = build_rows(Path(data_dir))
    Session = make_session_factory(database_url)          # creates any missing tables
    with Session.begin() as s:
        for model in reversed(TABLES):
            s.execute(delete(model))
        for model in TABLES:
            data = rows[model]
            for i in range(0, len(data), batch):
                s.execute(insert(model), data[i:i + batch])
    with Session() as s:
        return {m.__tablename__: s.scalar(select(func.count()).select_from(m)) for m in TABLES}


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    p = argparse.ArgumentParser(description="Load data/generated CSVs into the database")
    p.add_argument("--data", default=str(settings.data_dir))
    p.add_argument("--url", default=settings.database_url, help="SQLAlchemy URL (default: SA_DATABASE_URL)")
    args = p.parse_args(argv)

    if not (Path(args.data) / "accounts.csv").exists():
        print(f"No CSVs in {args.data}. Run `make data` first.", file=sys.stderr)
        return 1
    target = args.url.split("@")[-1]                       # don't print the password
    print(f"Loading {args.data} into {target} ...")
    for table, n in load(Path(args.data), args.url).items():
        print(f"  {table:22s} {n:>8,d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
