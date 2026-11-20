"""SA_DATA_SOURCE=db: the agent's loan data comes from the database, and edits there apply without a restart."""

from dataclasses import replace

from sqlalchemy import create_engine, text

from servicing_agent.data.db_loader import load
from servicing_agent.data.db_store import DbDataStore
from servicing_agent.data.store import DataStore


def _db(settings, tmp_path):
    url = f"sqlite:///{tmp_path}/src.db"
    load(settings.data_dir, url)
    return url


def test_same_shape_as_the_csv_store(settings, tmp_path):
    csv, db = DataStore(settings.data_dir), DbDataStore(_db(settings, tmp_path))
    a = next(iter(csv.accounts))
    for k in ("dpd", "dpd_band", "overdue_amount", "preapproved_settlement", "emi_amount", "next_due_date"):
        assert db.accounts[a][k] == csv.accounts[a][k], k
    c = next(iter(csv.contacts))
    assert db.contacts[c]["mobile"] == csv.contacts[c]["mobile"] and db.contacts[c]["is_active"] == csv.contacts[c]["is_active"]
    assert len(db.payments_by_utr) == len(csv.payments_by_utr)
    assert {l["contact_id"] for l in db.links_by_account[a]} == {l["contact_id"] for l in csv.links_by_account[a]}


def test_an_edit_in_the_database_is_used_after_the_refresh_window(settings, tmp_path):
    url = _db(settings, tmp_path)
    db = DbDataStore(url, refresh_seconds=0)
    a = next(iter(db.accounts))
    with create_engine(url).begin() as c:
        c.execute(text("update accounts set dpd = 15, dpd_band = '10-20' where account_id = :a"), {"a": a})
    assert db.accounts[a]["dpd"] == 15 and db.accounts[a]["dpd_band"] == "10-20"


def test_the_agent_answers_from_the_database(settings, tmp_path):
    from servicing_agent.platform.runtime import build_runtime
    from tests.test_agent_flow import say, verify
    url = _db(settings, tmp_path)
    rt = build_runtime(replace(settings, database_url=url, data_source="db", data_refresh_seconds=0))
    acc = next(a for a in rt.store.accounts.values()
               if any(l["role"] == "borrower" and rt.store.contacts[l["contact_id"]]["is_active"]
                      for l in rt.store.links_by_account[a["account_id"]]))
    link = next(l for l in rt.store.links_by_account[acc["account_id"]] if l["role"] == "borrower")
    sender = "91" + rt.store.contacts[link["contact_id"]]["mobile"]
    verify(rt, acc, sender)
    with create_engine(url).begin() as c:
        c.execute(text("update accounts set emi_amount = 4321.00 where account_id = :a"), {"a": acc["account_id"]})
    assert "4,321.00" in say(rt, sender, "what is my emi").replies[0].text
