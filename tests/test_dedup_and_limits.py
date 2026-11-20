"""The same receipt again (DUP-01) and a request already in approval (DUP-02): no duplicates, confirm before
re-initiating, and a hard stop on retries."""

from tests.test_agent_flow import say, verify
from tests.test_claim_authenticity import _image

UNPOSTED = dict(utr="818181818181", amount=1500.0, txn_date="2026-11-20")


def _run_recheck(runtime, sender, followup):
    return runtime.recheck_claim(runtime.conversation_id_for(sender), followup["slots"],
                                 followup["account_id"], followup["customer_id"])


def test_resending_a_receipt_while_it_is_checked_starts_no_second_check(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = _image(runtime, sender, **UNPOSTED)
    assert r.replies[0].template_id == "claim_checking" and len(r.followups) == 1
    r = _image(runtime, sender, **UNPOSTED)
    assert r.replies[0].template_id == "claim_still_checking" and not r.followups and not r.tickets


def test_resending_after_escalation_returns_the_same_reference(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    first = _image(runtime, sender, **UNPOSTED)
    esc = _run_recheck(runtime, sender, first.followups[0])
    ticket = esc.tickets[0]["ticket_id"]
    r = _image(runtime, sender, **UNPOSTED)
    assert r.replies[0].template_id == "claim_review_existing" and ticket in r.replies[0].text
    assert not r.tickets and not r.followups


def test_hard_stop_after_max_resends(runtime, borrower):
    from servicing_agent.agent.handlers import resend_limit
    a, sender = borrower
    verify(runtime, a, sender)
    first = _image(runtime, sender, **UNPOSTED)
    ticket = _run_recheck(runtime, sender, first.followups[0]).tickets[0]["ticket_id"]
    for _ in range(resend_limit() - 1):
        _image(runtime, sender, **UNPOSTED)
    r = _image(runtime, sender, **UNPOSTED)
    assert r.replies[0].template_id == "claim_resend_limit"
    assert ticket in r.replies[0].text and not r.tickets and not r.followups
    row = runtime.claims.get(a["account_id"], UNPOSTED["utr"])
    assert row["attempts"] == resend_limit() + 1                       # every send is still on the audit trail


def test_hard_stop_stays_neutral_after_a_silent_review(runtime, borrower):
    from servicing_agent.agent.handlers import resend_limit
    a, sender = borrower
    verify(runtime, a, sender)
    p = next(p for p in runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")
    edited = dict(utr=p["utr"], amount=p["amount"] + 3000, txn_date=p["txn_date"])
    tickets = list(_image(runtime, sender, **edited).tickets) + list(say(runtime, sender, "yes").tickets)
    for _ in range(resend_limit()):
        r = _image(runtime, sender, **edited)
        tickets += r.tickets
    assert len(tickets) == 1                                           # one compliance ticket, never more
    assert r.replies[0].template_id == "claim_resend_limit_neutral"
    text = r.replies[0].text.lower()
    assert not any(w in text for w in ("review", "escalat", "fraud", "flag", tickets[0]["ticket_id"].lower()))


def test_a_receipt_that_posts_is_confirmed_even_after_the_limit(runtime, borrower):
    from servicing_agent.agent.handlers import resend_limit
    from tests.test_claim_authenticity import _post_payment
    a, sender = borrower
    verify(runtime, a, sender)
    for _ in range(resend_limit() + 1):
        _image(runtime, sender, **UNPOSTED)
    _post_payment(runtime, a["account_id"], UNPOSTED["utr"], UNPOSTED["amount"], UNPOSTED["txn_date"])
    assert _image(runtime, sender, **UNPOSTED).replies[0].template_id == "claim_matched"


def test_escalation_request_is_deduplicated_on_the_open_ticket(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    say(runtime, sender, "please escalate this to the compliance team")
    r1 = say(runtime, sender, "yes")
    assert r1.replies[0].template_id == "escalation_raised" and len(r1.tickets) == 1
    r2 = say(runtime, sender, "can you escalate this to compliance team")
    assert r2.replies[0].template_id == "escalation_existing" and not r2.tickets
    assert r1.tickets[0]["ticket_id"] in r2.replies[0].text


def _open_waiver(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    say(runtime, sender, "please waive my penal charges of Rs 5000, my salary was delayed")
    assert say(runtime, sender, "yes").replies[0].template_id == "waiver_created"
    return a, sender


def test_same_request_again_points_to_the_active_approval(runtime, borrower):
    a, sender = _open_waiver(runtime, borrower)
    case = runtime.cases.open_case_for_account(a["account_id"])
    r = say(runtime, sender, "please waive my penal charges of Rs 5000, my salary was delayed")
    assert r.replies[0].template_id == "case_active" and not r.tickets
    assert case.case_id in r.replies[0].text and "supervisor" in r.replies[0].text
    assert runtime.cases.open_case_for_account(a["account_id"]).version == 1


def test_a_change_is_confirmed_as_restarting_the_approval(runtime, borrower):
    a, sender = _open_waiver(runtime, borrower)
    r = say(runtime, sender, "can you make the waiver Rs 3000")
    assert r.replies[0].template_id == "waiver_confirm_change"
    assert "restarts the approval" in r.replies[0].text and "YES" in r.replies[0].text
    assert say(runtime, sender, "no").replies[0].template_id == "waiver_change_kept"
    assert runtime.cases.open_case_for_account(a["account_id"]).version == 1


def test_hard_stop_after_max_revisions(runtime, borrower):
    from servicing_agent.agent.handlers import revision_limit
    a, sender = _open_waiver(runtime, borrower)
    amounts = [4000 - 500 * i for i in range(revision_limit() + 1)]
    for amt in amounts[:-1]:
        r = say(runtime, sender, f"can you make the waiver Rs {amt}")
        assert r.replies[0].template_id == "waiver_confirm_change"
        assert say(runtime, sender, "yes").replies[0].template_id == "waiver_restarted"
    assert "last change" in r.replies[0].text                       # warned before the final change
    r = say(runtime, sender, f"can you make the waiver Rs {amounts[-1]}")
    assert r.replies[0].template_id == "case_revision_limit"
    case = runtime.cases.open_case_for_account(a["account_id"])
    assert case.version == revision_limit() + 1 and case.request.amount == amounts[-2]
