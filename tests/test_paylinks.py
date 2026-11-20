"""Mock pay-link payment: create -> pay -> overdue deducted + UTR/feed/receipt, idempotent, replay."""

import uuid

from servicing_agent.contracts.state import InboundMessage
from servicing_agent.platform.paylinks import PayLinkService


def _verified_turn(runtime, sender, account_id):
    def turn(text):
        cid = runtime.conversation_id_for(sender)
        r = runtime.run_turn(InboundMessage(message_id="t" + uuid.uuid4().hex[:8], sender=sender,
                                            type="text", text=text), cid)
        runtime.conversations.persist(cid)
        return r
    turn("show my account status"); turn(account_id); turn("123456")   # agent-OTP verify (stub)
    return turn


def _has_link(result):
    return any("/pay/" in (x.text or "") for x in result.replies)


def test_pay_now_skips_link_when_no_overdue_unless_emi_requested(runtime, borrower):
    acc, sender = borrower
    aid = acc["account_id"]
    turn = _verified_turn(runtime, sender, aid)
    assert runtime.conversations.get(runtime.conversation_id_for(sender))["session"].get("scope")

    runtime.store.accounts[aid]["overdue_amount"] = 0.0
    assert not _has_link(turn("i want to pay now"))                     # ₹0 overdue -> no link pushed
    assert _has_link(turn("send me the payment link for my emi"))       # explicit EMI -> link sent


def test_pay_now_sends_link_when_overdue(runtime, borrower):
    acc, sender = borrower
    aid = acc["account_id"]
    turn = _verified_turn(runtime, sender, aid)
    runtime.store.accounts[aid]["overdue_amount"] = 2500.0
    assert _has_link(turn("i want to pay now"))


def test_mock_payment_deducts_overdue_and_feeds_reconciliation(runtime, borrower):
    acc, sender = borrower
    aid = acc["account_id"]
    store = runtime.store
    store.accounts[aid]["overdue_amount"] = 5000.0            # known starting overdue
    cid = runtime.conversation_id_for(sender)

    link = runtime.paylinks.create(aid, 5000.0, cid)
    assert link["payment_link"].endswith("/pay/" + link["link_id"])
    assert link["amount"] == 5000.0

    res = runtime.paylinks.pay(link["link_id"])
    assert res["status"] == "paid"
    assert res["overdue_after"] == 0.0
    assert store.accounts[aid]["overdue_amount"] == 0.0       # deducted in the live store
    assert store.accounts[aid]["dpd"] == 0                    # cleared -> DPD 0
    assert res["utr"] in store.payments_by_utr                # now reconcilable
    assert any(p["utr"] == res["utr"] for p in store.payments_by_account[aid])
    assert res["receipt"] and res["receipt"].get("path")      # receipt issued


def test_mock_payment_is_idempotent(runtime, borrower):
    acc, sender = borrower
    aid = acc["account_id"]
    runtime.store.accounts[aid]["overdue_amount"] = 3000.0
    link = runtime.paylinks.create(aid, 1000.0, runtime.conversation_id_for(sender))
    first = runtime.paylinks.pay(link["link_id"])
    second = runtime.paylinks.pay(link["link_id"])
    assert first["status"] == "paid" and second["status"] == "already_paid"
    assert runtime.store.accounts[aid]["overdue_amount"] == 2000.0   # charged once, not twice


def test_new_link_supersedes_the_previous_pending_one(runtime, borrower):
    acc, sender = borrower
    aid = acc["account_id"]
    cid = runtime.conversation_id_for(sender)
    old = runtime.paylinks.create(aid, 1000.0, cid)
    runtime.paylinks.create(aid, 2000.0, cid)                 # supersedes the old one
    assert runtime.paylinks.get(old["link_id"])["status"] == "superseded"
    assert runtime.paylinks.pay(old["link_id"])["status"] == "superseded"   # can't pay a stale link


def test_paid_links_replay_onto_a_fresh_store(runtime, borrower):
    """A restart rebuilds the store from CSV; paid links must re-apply their overdue delta + feed."""
    acc, sender = borrower
    aid = acc["account_id"]
    runtime.store.accounts[aid]["overdue_amount"] = 4000.0
    link = runtime.paylinks.create(aid, 4000.0, runtime.conversation_id_for(sender))
    paid = runtime.paylinks.pay(link["link_id"])

    # Simulate a restart: a brand-new service over a fresh store reading the same CSV.
    from servicing_agent.data.store import DataStore
    fresh = DataStore(runtime.settings.data_dir)
    base_overdue = float(fresh.accounts[aid]["overdue_amount"])   # full overdue from CSV
    svc = PayLinkService(runtime.session_factory, fresh)
    n = svc.replay_paid()
    assert n >= 1
    assert fresh.accounts[aid]["overdue_amount"] == round(base_overdue - 4000.0, 2)
    assert paid["utr"] in fresh.payments_by_utr                 # feed entry restored too
