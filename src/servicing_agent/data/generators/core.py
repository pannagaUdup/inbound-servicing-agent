"""D1 (customers, accounts), D2 (contacts, account_contacts), D7 (payments), waiver history.

All generation is driven by one seeded ``random.Random`` so a given (seed, size)
always produces identical files. Distributions follow docs/DESIGN.md section 4.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from servicing_agent.data.identifiers import make_account_number
from servicing_agent.data.store import dpd_band

PRODUCTS = ["personal", "business", "two_wheeler", "consumer_durable"]
LANGS = ["en", "hi", "kn", "ta", "te", "mr", "bn"]
FIRST = ["Aarav", "Vivaan", "Ananya", "Diya", "Ishaan", "Kavya", "Rohan", "Sneha", "Arjun", "Meera",
         "Rahul", "Pooja", "Karthik", "Lakshmi", "Imran", "Fatima", "Suresh", "Divya", "Manoj", "Nisha"]
LAST = ["Sharma", "Patel", "Reddy", "Iyer", "Khan", "Das", "Nair", "Gowda", "Singh", "Joshi",
        "Rao", "Mehta", "Banerjee", "Pillai", "Kulkarni"]
CITIES = ["Bengaluru", "Mumbai", "Delhi", "Chennai", "Hyderabad", "Pune", "Kolkata", "Jaipur", "Lucknow", "Kochi"]
OUR_VPA = "collect@cnbank"

DPD_BUCKETS = [((0, 0), 0.40), ((1, 30), 0.25), ((31, 60), 0.15), ((61, 90), 0.10), ((91, 360), 0.10)]
NUMBERS_PER_CUSTOMER = [(1, 0.60), (2, 0.25), (3, 0.10), (4, 0.05)]


def _weighted(rng: random.Random, options):
    r, acc = rng.random(), 0.0
    for value, weight in options:
        acc += weight
        if r <= acc:
            return value
    return options[-1][0]


@dataclass
class Dataset:
    customers: list[dict]
    accounts: list[dict]
    contacts: list[dict]
    account_contacts: list[dict]
    payments: list[dict]
    waiver_history: list[dict]


def generate(n_accounts: int = 10_000, seed: int = 42, as_of: date | None = None) -> Dataset:
    rng = random.Random(seed)
    as_of = as_of or date.today()

    # --- D1: customers and accounts (~15% of customers hold more than one account)
    customers: list[dict] = []
    accounts: list[dict] = []
    used_numbers: set[str] = set()
    cust_seq = 0
    while len(accounts) < n_accounts:
        cust_seq += 1
        cid = f"CUST{cust_seq:07d}"
        dob = date(1960, 1, 1) + timedelta(days=rng.randint(0, 365 * 42))
        customers.append({
            "customer_id": cid,
            "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "dob": dob.isoformat(),
            "preferred_language": rng.choices(LANGS, weights=[30, 30, 10, 8, 8, 8, 6])[0],
            "city": rng.choice(CITIES),
        })
        n_acc = 1 if rng.random() > 0.15 else rng.choice([2, 2, 3])
        for _ in range(n_acc):
            if len(accounts) >= n_accounts:
                break
            while True:
                acc_no = make_account_number(f"{rng.randint(0, 999_999_999):09d}")
                if acc_no not in used_numbers:
                    used_numbers.add(acc_no)
                    break
            product = rng.choice(PRODUCTS)
            sanction = round(rng.uniform(20_000, 1_500_000 if product == "business" else 500_000), -2)
            tenure = rng.choice([12, 18, 24, 36, 48, 60])
            tenure_left = rng.randint(1, tenure)
            rate = round(rng.uniform(11, 26), 2)
            emi = round(sanction * (rate / 1200) / (1 - (1 + rate / 1200) ** -tenure), 2)
            principal = round(sanction * tenure_left / tenure, 2)
            lo, hi = _weighted(rng, DPD_BUCKETS)
            dpd = rng.randint(lo, hi)
            missed = (dpd + 29) // 30
            overdue = round(emi * missed, 2)
            bounce = 590.0 * min(missed, 6)
            penal = round(overdue * 0.02 * missed, 2)
            accounts.append({
                "account_id": acc_no,
                "customer_id": cid,
                "product": product,
                "sanction_amount": sanction,
                "principal_outstanding": principal,
                "emi_amount": emi,
                "interest_rate": rate,
                "tenure_months": tenure,
                "tenure_left": tenure_left,
                "dpd": dpd,
                "dpd_band": dpd_band(dpd),
                "preapproved_settlement": "Y" if sanction and (sanction - principal) / sanction >= 0.9 else "N",
                "overdue_amount": overdue,
                "bounce_charges": bounce,
                "penal_charges": penal,
                "foreclosure_charge_pct": rng.choice([0, 2, 3, 4]),
                "next_due_date": (as_of.replace(day=5) + timedelta(days=31)).replace(day=5).isoformat(),
                "status": "active",
                # Internal-only fields the gateway must never expose:
                "collector_name": f"{rng.choice(FIRST)} (agency {rng.randint(1, 40)})",
                "internal_notes": rng.choice(["", "PTP broken twice", "skip-trace pending", "field visit scheduled"]),
                "bureau_score": rng.randint(480, 820),
            })

    # --- D2: contacts. 60/25/10/5% numbers per customer, ~2% shared, ~10% inactive, ~3% added in last 7 days
    contacts: list[dict] = []
    mobiles: list[str] = []
    contact_seq = 0

    def new_mobile() -> str:
        while True:
            m = f"{rng.choice('6789')}{rng.randint(0, 999_999_999):09d}"
            if m not in mobiles_set:
                mobiles_set.add(m)
                return m

    mobiles_set: set[str] = set()
    for c in customers:
        for _ in range(_weighted(rng, NUMBERS_PER_CUSTOMER)):
            contact_seq += 1
            shared = mobiles and rng.random() < 0.02
            mobile = rng.choice(mobiles) if shared else new_mobile()
            mobiles.append(mobile)
            recent = rng.random() < 0.03
            added = as_of - timedelta(days=rng.randint(0, 6) if recent else rng.randint(8, 1500))
            contacts.append({
                "contact_id": f"CT{contact_seq:08d}",
                "customer_id": c["customer_id"],
                "mobile": mobile,
                "is_active": rng.random() >= 0.10,
                "added_on": added.isoformat(),
            })
    # Guarantee every customer has at least one active contact (keeps scenarios solvable).
    by_cust: dict[str, list[dict]] = {}
    for ct in contacts:
        by_cust.setdefault(ct["customer_id"], []).append(ct)
    for cts in by_cust.values():
        if not any(ct["is_active"] for ct in cts):
            cts[0]["is_active"] = True

    account_contacts: list[dict] = []
    for a in accounts:
        for ct in by_cust[a["customer_id"]]:
            account_contacts.append({"account_id": a["account_id"], "contact_id": ct["contact_id"], "role": "borrower"})
        if rng.random() < 0.03:  # co-borrower: another customer's contact linked to this account
            other = rng.choice(contacts)
            if other["customer_id"] != a["customer_id"]:
                account_contacts.append({"account_id": a["account_id"], "contact_id": other["contact_id"], "role": "co_borrower"})

    # --- D7: payments over 6 months (~4 per account), unique 12-digit UTRs, ~1% suspense
    payments: list[dict] = []
    utrs: set[str] = set()
    start = as_of - timedelta(days=180)
    txn_seq = 0
    for a in accounts:
        paid_months = rng.randint(2, 6) if a["dpd"] < 31 else rng.randint(0, 3)
        for k in range(paid_months):
            txn_seq += 1
            while True:
                utr = f"{rng.randint(10**11, 10**12 - 1)}"
                if utr not in utrs:
                    utrs.add(utr)
                    break
            txn_date = start + timedelta(days=30 * k + rng.randint(0, 25))
            status = rng.choices(["success", "failed", "pending", "reversed"], weights=[90, 6, 2, 2])[0]
            lag = rng.choices([0, 1, 2], weights=[70, 25, 5])[0]
            suspense = rng.random() < 0.01
            payments.append({
                "txn_id": f"TXN{txn_seq:09d}",
                "utr": utr,
                "account_id": "" if suspense else a["account_id"],
                "amount": a["emi_amount"] if rng.random() > 0.2 else round(a["emi_amount"] * rng.uniform(0.3, 1.5), 2),
                "channel": rng.choices(["UPI", "NACH", "NEFT", "CASH"], weights=[55, 30, 10, 5])[0],
                "status": status,
                "txn_date": txn_date.isoformat(),
                "settled_date": (txn_date + timedelta(days=lag)).isoformat(),
                "payee_vpa": OUR_VPA,
            })

    # --- Waiver/settlement history on ~5% of accounts
    waiver_history: list[dict] = []
    for a in accounts:
        if rng.random() < 0.05:
            waiver_history.append({
                "account_id": a["account_id"],
                "type": rng.choice(["waiver", "settlement"]),
                "amount": round(rng.uniform(500, 25_000), 2),
                "components": rng.choice(["penal", "penal|bounce", "interest"]),
                "decided_on": (as_of - timedelta(days=rng.randint(30, 700))).isoformat(),
                "outcome": rng.choice(["approved", "rejected"]),
            })

    return Dataset(customers, accounts, contacts, account_contacts, payments, waiver_history)


def write_csvs(ds: Dataset, out: Path) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name in ["customers", "accounts", "contacts", "account_contacts", "payments", "waiver_history"]:
        rows = getattr(ds, name)
        counts[name] = len(rows)
        if not rows:
            # still write the header so loaders work
            rows_header = {"waiver_history": ["account_id", "type", "amount", "components", "decided_on", "outcome"]}.get(name, [])
            with open(out / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh, lineterminator="\n").writerow(rows_header)
            continue
        with open(out / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
    return counts


def validate(ds: Dataset) -> list[str]:
    """Referential-integrity checks. Returns a list of problems (empty = valid)."""
    problems = []
    cust_ids = {c["customer_id"] for c in ds.customers}
    acc_ids = {a["account_id"] for a in ds.accounts}
    contact_ids = {c["contact_id"] for c in ds.contacts}
    problems += [f"account {a['account_id']} -> unknown customer" for a in ds.accounts if a["customer_id"] not in cust_ids]
    problems += [f"contact {c['contact_id']} -> unknown customer" for c in ds.contacts if c["customer_id"] not in cust_ids]
    problems += [f"link {l['account_id']}/{l['contact_id']} broken" for l in ds.account_contacts
                 if l["account_id"] not in acc_ids or l["contact_id"] not in contact_ids]
    problems += [f"payment {p['txn_id']} -> unknown account" for p in ds.payments if p["account_id"] and p["account_id"] not in acc_ids]
    if len({p["utr"] for p in ds.payments}) != len(ds.payments):
        problems.append("duplicate UTRs")
    return problems
