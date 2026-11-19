from sqlalchemy import select

from servicing_agent.data.db_loader import load
from servicing_agent.platform.campaigns import mobile_hash
from servicing_agent.platform.db import Account, Contact, PaymentTransaction, make_session_factory


def test_loader_is_idempotent_and_hashes_mobiles(data_dir, tmp_path, runtime):
    url = f"sqlite:///{tmp_path}/load.db"
    first = load(data_dir, url)
    second = load(data_dir, url)                       # re-run replaces, doesn't duplicate
    assert first == second
    store = runtime.store
    assert first["accounts"] == len(store.accounts)
    assert first["payment_transactions"] == len(store.payments)
    assert first["contacts"] == len(store.contacts)

    Session = make_session_factory(url)
    with Session() as s:
        ct = next(iter(store.contacts.values()))
        row = s.get(Contact, ct["contact_id"])
        assert row.mobile_hash == mobile_hash(ct["mobile"])            # same hash the campaign service uses
        assert ct["mobile"] not in row.mobile_hash                       # raw number never stored
        suspense = s.scalars(select(PaymentTransaction).where(PaymentTransaction.account_id.is_(None))).all()
        assert all(p.utr for p in suspense)                              # "" became NULL, not ""
        acc = s.get(Account, next(iter(store.accounts)))
        assert acc.internal_notes is None or acc.internal_notes != ""
