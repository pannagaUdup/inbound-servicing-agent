"""Unmatched payment claims: authenticity score, payment age, acknowledge-and-recheck vs escalation."""

from datetime import datetime, timedelta

from servicing_agent.agent.graph import new_message_id
from servicing_agent.agent.handlers import payment_age_minutes
from servicing_agent.agent.tools.authenticity import score_claim
from servicing_agent.agent.tools.reconciliation import ClaimResult, Verdict, india_now
from servicing_agent.contracts.state import InboundMessage, MediaEvidence

from tests.test_agent_flow import say, verify

LOAN = {"emi_amount": 1481.99, "overdue_amount": 11855.92}


def test_edited_amount_on_a_real_utr_scores_as_fake():
    txn = {"account_id": "CN1", "amount": 1481.99, "txn_date": "2026-11-19", "utr": "512398765432"}
    a = score_claim({"utr": "512398765432", "amount": 4481.99, "txn_date": "2026-11-19", "payee_vpa": "collect@cnbank",
                     "status": "success", "txn_time": "10:15"},
                    ClaimResult(Verdict.MISMATCH, ["amount differs from ledger"]), txn, "CN1", LOAN, confidence=0.95)
    assert a.label == "likely_fake" and a.score <= 0.2
    assert a.signals["amount_differs_from_ledger"] < 0


def test_fresh_plausible_payment_is_supported():
    a = score_claim({"utr": "999988887777", "amount": 1481.99, "txn_date": "2026-11-20", "payee_vpa": "collect@cnbank",
                     "status": "success", "txn_time": "01:50"},
                    ClaimResult(Verdict.PENDING, ["not in D7 yet"]), None, "CN1", LOAN, confidence=0.95)
    assert a.label == "supported"


def test_someone_elses_utr_is_likely_fake():
    txn = {"account_id": "CN2", "amount": 1481.99, "txn_date": "2026-11-19"}
    a = score_claim({"utr": "512398765432", "amount": 1481.99, "txn_date": "2026-11-19"},
                    ClaimResult(Verdict.OTHER_ACCOUNT, []), txn, "CN1", LOAN)
    assert a.label == "likely_fake"


def test_payment_age():
    # 2026-11-19 20:45 UTC = 2026-11-20 02:15 IST
    at = datetime(2026, 11, 19, 20, 45)
    assert payment_age_minutes({"txn_date": "2026-11-20", "txn_time": "02:00"}, at) == 15
    assert payment_age_minutes({"txn_date": "2026-11-19", "txn_time": "10:15"}, at) == 16 * 60
    assert payment_age_minutes({"txn_date": "2026-11-20"}, at) is None          # today, time unknown
    assert payment_age_minutes({}, at) is None


# ---------------- end to end

def _image(runtime, sender, **receipt):
    return runtime.run_turn(InboundMessage(message_id=new_message_id(), sender=sender, type="image",
                                           media=MediaEvidence(kind="upi_receipt", provider="fake", **receipt)))


def _fresh_receipt(minutes_ago: int, utr: str, amount: float) -> dict:
    t = india_now() - timedelta(minutes=minutes_ago)
    return {"utr": utr, "amount": amount, "txn_date": t.date().isoformat(), "txn_time": t.strftime("%H:%M"),
            "payee_vpa": "collect@cnbank", "status": "success"}


def _post_payment(runtime, account_id, utr, amount, txn_date):
    row = {"txn_id": f"TXN{utr[-6:]}", "utr": utr, "account_id": account_id, "amount": amount, "channel": "UPI",
           "status": "success", "txn_date": txn_date, "settled_date": txn_date, "payee_vpa": "collect@cnbank"}
    runtime.store.payments_by_utr[utr] = row                              # the LMS / bank feed catches up
    runtime.store.payments_by_account[account_id].append(row)


NEUTRAL_FORBIDDEN = ("escalat", "fraud", "fake", "compliance", "review", "flag", "tamper", "edited")


def test_edited_receipt_is_escalated_silently(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    p = next(p for p in runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    r = _image(runtime, sender, utr=p["utr"], amount=p["amount"] + 3000, txn_date=p["txn_date"], txn_time="10:15")
    reply = r.replies[0]
    assert reply.template_id == "claim_unverified"
    assert not any(w in reply.text.lower() for w in NEUTRAL_FORBIDDEN)          # borrower isn't told
    t = r.tickets[0]
    assert t["queue"] == "fraud_review" and t["summary"]["borrower_informed"] is False
    assert t["summary"]["authenticity"]["label"] == "likely_fake"

    again = _image(runtime, sender, utr=p["utr"], amount=p["amount"] + 3000, txn_date=p["txn_date"])
    assert again.replies[0].template_id == "claim_unverified" and not again.tickets   # no second ticket


def test_someone_elses_utr_is_escalated_silently(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    other = next(p for p in runtime.store.payments if p["account_id"] and p["account_id"] != a["account_id"])
    r = _image(runtime, sender, utr=other["utr"], amount=other["amount"], txn_date=other["txn_date"])
    assert r.replies[0].template_id == "claim_unverified" and r.tickets[0]["queue"] == "fraud_review"
    assert other["account_id"] not in r.replies[0].text


def test_missing_payment_waits_two_minutes_then_fails_with_escalation(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = _image(runtime, sender, **_fresh_receipt(1, "998877665544", 1500.0))
    assert r.replies[0].template_id == "claim_checking" and "2 minutes" in r.replies[0].text and not r.tickets
    f = r.followups[0]
    assert f["kind"] == "recheck_claim" and f["delay_seconds"] == 120

    late = runtime.recheck_claim(r.conversation_id, f["slots"])
    reply = late.replies[0]
    assert reply.template_id == "claim_failed_escalated" and late.tickets[0]["ticket_id"] in reply.text
    assert "couldn't confirm" in reply.text and late.tickets[0]["queue"] == "payment_reconciliation"
    assert runtime.recheck_claim(r.conversation_id, f["slots"]).replies == []    # a duplicate recheck: silent


def test_old_receipt_not_in_records_also_gets_the_window(runtime, borrower):
    """The 2 minutes run from when it was sent, whatever the receipt's own date."""
    a, sender = borrower
    verify(runtime, a, sender)
    r = _image(runtime, sender, utr="887766554433", amount=1500.0, txn_date="2026-11-01", txn_time="10:15")
    assert r.replies[0].template_id == "claim_checking" and r.followups


def test_payment_that_posts_within_the_window_is_confirmed(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    rec = _fresh_receipt(1, "112233445566", 1500.0)
    r = _image(runtime, sender, **rec)
    _post_payment(runtime, a["account_id"], rec["utr"], rec["amount"], rec["txn_date"])
    again = runtime.recheck_claim(r.conversation_id, r.followups[0]["slots"])
    assert again.replies[0].template_id == "claim_matched" and not again.tickets


def test_recheck_is_silent_if_a_resend_already_confirmed_it(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    rec = _fresh_receipt(1, "223344556677", 1500.0)
    r = _image(runtime, sender, **rec)
    _post_payment(runtime, a["account_id"], rec["utr"], rec["amount"], rec["txn_date"])
    assert _image(runtime, sender, **rec).replies[0].template_id == "claim_matched"     # resent, now posted
    assert runtime.recheck_claim(r.conversation_id, r.followups[0]["slots"]).replies == []


def test_typed_utr_follows_the_same_window(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "paid UTR 667788990011")
    assert r.replies[0].template_id == "claim_checking"
    assert runtime.recheck_claim(r.conversation_id, r.followups[0]["slots"]).replies[0].template_id == \
        "claim_failed_escalated"


def test_recheck_survives_a_lost_session(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = _image(runtime, sender, **_fresh_receipt(1, "334455667788", 1500.0))
    f = r.followups[0]
    runtime.conversations.load(r.conversation_id)["session"]["scope"] = None      # e.g. after a restart
    assert runtime.recheck_claim(r.conversation_id, f["slots"]) is None
    res = runtime.recheck_claim(r.conversation_id, f["slots"], f["account_id"], f["customer_id"])
    assert res.replies[0].template_id == "claim_failed_escalated"


def test_worker_sends_the_result_when_the_window_ends(settings, borrower):
    import time
    from dataclasses import replace
    from tests.test_worker_pipeline import FakePerception, msg, setup
    settings = replace(settings, claim_window_seconds=1)
    rec = _fresh_receipt(1, "554433221100", 1500.0)
    app, rt, sender = setup(settings, borrower, FakePerception(receipts={b"IMG": dict(rec)}), {"M1": b"IMG"})
    app.state.process_batch([msg("w1", sender, type="image", media_id="M1")])
    texts = lambda: [m["payload"]["text"]["body"] for m in rt.channel.sent if "text" in m["payload"]]
    deadline = time.time() + 4
    while time.time() < deadline and not any("Reference" in t for t in texts()):
        time.sleep(0.05)
    assert "check again in 1 minute" not in texts()[0] and "isn't showing" in texts()[0]
    assert any("couldn't confirm" in t for t in texts())


class FakeSyncRedis:
    def __init__(self):
        self.z = {}

    def zadd(self, key, mapping):
        self.z.update(mapping)

    def zrangebyscore(self, key, lo, hi, start=0, num=50):
        return [m for m, s in sorted(self.z.items(), key=lambda x: x[1]) if lo <= s <= hi][:num]

    def zrem(self, key, member):
        return 1 if self.z.pop(member, None) is not None else 0

    def zcard(self, key):
        return len(self.z)


def test_scheduler_keeps_jobs_in_redis_and_runs_each_once():
    import time
    from servicing_agent.platform.rechecks import RecheckScheduler
    ran = []
    s = RecheckScheduler(ran.append, FakeSyncRedis())
    s.schedule({"conversation_id": "wa:1", "slots": {"utr": "1"}}, 60)
    s.schedule({"conversation_id": "wa:2", "slots": {"utr": "2"}}, 0)
    assert s.durable and s.pending() == 2
    assert s.run_due() == 1 and [j["conversation_id"] for j in ran] == ["wa:2"]
    assert s.run_due() == 0                                                         # not twice
    assert s.run_due(now=time.time() + 61) == 1 and s.pending() == 0


