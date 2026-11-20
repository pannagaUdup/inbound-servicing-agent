"""Payment reminder (console button) and payments reaching the ledger after a screenshot claim.

Screenshot scenarios:
  3   genuine, already in the ledger        -> confirmed now, receipt
  1   genuine, not in the ledger yet        -> "checking, 2 minutes"
  1.1 reaches the ledger (before or after the escalation) -> confirmed, receipt, escalation ticket closed
  1.2 still missing after 2 minutes         -> escalated to compliance, borrower gets the reference + what was checked
  2   fake / edited                         -> compliance ticket with the evidence; borrower not told
"""

from fastapi.testclient import TestClient

from servicing_agent.contracts.state import MediaEvidence
from tests.test_claim_authenticity import _fresh_receipt
from tests.test_reverify_flow import _app, _bodies, _multi_loan_sender, _send, _verify_via_chooser

import itertools

_utr = (f"77{n:010d}" for n in itertools.count(4600000001))     # a range no other test uses


def _unposted():
    """A receipt not in the ledger, with its own UTR and amount (Post to ledger adds to the shared test data, and
    a claim also matches a ledger payment of the same amount and date)."""
    utr = next(_utr)
    return _fresh_receipt(1, utr, 1500.0 + int(utr[-3:]))


def _verified(settings):
    app, rt = _app(settings)
    sender, accounts = _multi_loan_sender(rt)
    acc = accounts[0]
    _send(app, sender, "hi")
    _send(app, sender, "what is my emi")
    _verify_via_chooser(app, rt, sender, acc)
    rt.channel.sent.clear()
    return app, rt, sender, acc, TestClient(app)


def _receipt(app, sender, r):
    _send(app, sender, "check this payment", type="image",
          media=MediaEvidence(kind="upi_receipt", provider="fake", **{k: r[k] for k in ("utr", "amount", "txn_date", "txn_time", "payee_vpa")}))


def _rows(rt, sender, utr):
    return rt.claims.get(next(a for a in rt.store.accounts if rt.claims.get(a, utr)), utr)


# ---------------- screenshots

def test_1_1_reaches_the_ledger_within_the_window(settings):
    app, rt, sender, acc, client = _verified(settings)
    U = _unposted()
    _receipt(app, sender, U)
    assert any("isn't showing in our records yet" in b for b in _bodies(rt))
    rt.channel.sent.clear()
    r = client.post("/admin/api/claims/post-to-ledger", json={"account_id": acc, "utr": U["utr"]}).json()
    assert r["posted"] and r["confirmed_claims"] == 1
    assert any("has now reached us and is confirmed" in b for b in _bodies(rt))
    assert any(m["kind"] == "document" for m in rt.channel.sent)                                  # the receipt PDF
    assert rt.claims.get(acc, U["utr"])["result"] == "MATCHED"


def test_1_2_escalated_to_compliance_with_context_then_1_1_late_closes_it(settings):
    app, rt, sender, acc, client = _verified(settings)
    U = _unposted()
    _receipt(app, sender, U)
    cid = rt.conversation_id_for(sender)
    late = rt.recheck_claim(cid, {**U, "recheck": True, "source": "screenshot"}, acc)   # 2 minutes later
    ticket = late.tickets[0]
    assert ticket["queue"] == "compliance_review"
    assert "we checked it against our records at" in late.replies[0].text and ticket["ticket_id"] in late.replies[0].text
    rt.channel.sent.clear()
    client.post("/admin/api/claims/post-to-ledger", json={"account_id": acc, "utr": U["utr"]})
    body = next(b for b in _bodies(rt) if "has now reached us" in b)
    assert f"The review {ticket['ticket_id']} is closed" in body
    assert rt.tools.tickets.status_of(ticket["ticket_id"]) == "resolved"


def test_recheck_after_the_watcher_confirmed_it_says_nothing(settings):
    app, rt, sender, acc, client = _verified(settings)
    U = _unposted()
    _receipt(app, sender, U)
    client.post("/admin/api/claims/post-to-ledger", json={"account_id": acc, "utr": U["utr"]})
    late = rt.recheck_claim(rt.conversation_id_for(sender), {**U, "recheck": True, "source": "screenshot"}, acc)
    assert late.replies == [] and not late.tickets                                               # no double message


def test_2_fake_goes_to_compliance_with_evidence_and_the_borrower_is_not_told(settings):
    app, rt, sender, acc, client = _verified(settings)
    p = next(x for x in rt.store.payments_by_account[acc] if x["status"] == "success")
    edited = {**_fresh_receipt(1, p["utr"], float(p["amount"]) + 3000), "txn_date": p["txn_date"]}
    _receipt(app, sender, edited)
    assert any("Is this the right screenshot?" in b for b in _bodies(rt))                     # double-check first
    _send(app, sender, "yes")
    t = next(t for t in rt.tools.tickets.list() if t["request_type"] == "payment_mismatch")
    assert t["queue"] == "compliance_review"
    s = t["summary"]
    assert s["claimed"]["utr"] == p["utr"] and s["authenticity"] and s["verdict"] and s["borrower_informed"] is False
    text = " ".join(_bodies(rt)).lower()
    assert not any(w in text for w in ("fraud", "fake", "escalat", "compliance", t["ticket_id"].lower()))


def test_3_genuine_and_in_the_ledger_is_confirmed_with_the_receipt(settings):
    app, rt, sender, acc, client = _verified(settings)
    p = next(x for x in rt.store.payments_by_account[acc] if x["status"] == "success")
    _receipt(app, sender, {"utr": p["utr"], "amount": float(p["amount"]), "txn_date": p["txn_date"], "txn_time": None,
                           "payee_vpa": p["payee_vpa"]})
    assert any("confirmed" in b.lower() for b in _bodies(rt)) and any(m["kind"] == "document" for m in rt.channel.sent)


def test_post_to_ledger_twice_or_for_an_unknown_claim_is_refused(settings):
    app, rt, sender, acc, client = _verified(settings)
    U = _unposted()
    _receipt(app, sender, U)
    assert client.post("/admin/api/claims/post-to-ledger", json={"account_id": acc, "utr": U["utr"]}).status_code == 200
    assert client.post("/admin/api/claims/post-to-ledger", json={"account_id": acc, "utr": U["utr"]}).status_code == 409
    assert client.post("/admin/api/claims/post-to-ledger", json={"account_id": acc, "utr": "999999999999"}).status_code == 404


# ---------------- reminder

def test_reminder_is_sent_with_three_buttons(settings):
    app, rt, sender, acc, client = _verified(settings)
    r = client.post(f"/admin/api/reminders/{acc}")
    assert r.status_code == 200
    m = rt.channel.sent[-1]
    assert m["kind"] == "buttons"
    body = m["payload"]["interactive"]["body"]["text"]
    assert acc[-4:] in body and acc not in body and "₹" in body                                  # masked loan, amount
    ids = [b["reply"]["id"] for b in m["payload"]["interactive"]["action"]["buttons"]]
    assert ids == ["REMIND_PAY", "REMIND_PAID", "REMIND_CANT"]


def test_pay_now_needs_no_code_and_sends_a_link(settings):
    app, rt, sender, acc, client = _verified(settings)
    rt.end_session(rt.conversation_id_for(sender), "test")                                       # not verified
    client.post(f"/admin/api/reminders/{acc}")
    rt.channel.sent.clear()
    _send(app, sender, "REMIND_PAY", type="button")
    assert any("You can pay ₹" in b and "/pay/" in b for b in _bodies(rt))


def test_already_paid_asks_for_the_code_for_that_loan_then_asks_for_the_receipt(settings):
    app, rt, sender, acc, client = _verified(settings)
    rt.end_session(rt.conversation_id_for(sender), "test")
    client.post(f"/admin/api/reminders/{acc}")
    rt.channel.sent.clear()
    _send(app, sender, "REMIND_PAID", type="button")
    assert any("emailed a verification code" in b for b in _bodies(rt))                         # no account number asked
    assert not any("account number" in b.lower() for b in _bodies(rt))
    rt.channel.sent.clear()
    _send(app, sender, "424242")                                                                 # the code
    sent = _bodies(rt)
    assert any("Verified" in b for b in sent) and any("UTR" in b or "screenshot" in b for b in sent)
    assert rt.conversations.load(rt.conversation_id_for(sender))["session"]["scope"]["account_id"] == acc


def test_cant_pay_when_already_verified_goes_straight_to_the_conversation(settings):
    app, rt, sender, acc, client = _verified(settings)
    client.post(f"/admin/api/reminders/{acc}")
    rt.channel.sent.clear()
    _send(app, sender, "REMIND_CANT", type="button")
    assert not any("verification code" in b for b in _bodies(rt)) and _bodies(rt)


def test_reminder_to_a_number_that_never_messaged_us_is_refused(settings):
    app, rt = _app(settings)
    client = TestClient(app)
    acc = next(a for a in rt.store.accounts
               if rt.conversations.get(f"wa:{rt.store.contacts[rt.store.links_by_account[a][0]['contact_id']]['mobile']}") is None)
    r = client.post(f"/admin/api/reminders/{acc}")
    assert r.status_code == 409 and not rt.channel.sent


def test_a_button_without_a_reminder_is_harmless(settings):
    app, rt, sender, acc, client = _verified(settings)
    _send(app, sender, "REMIND_PAY", type="button")
    assert any("no longer active" in b for b in _bodies(rt))


# ---------------- messages around verification

def test_a_message_sent_while_the_code_is_pending_is_answered_after_it(settings):
    app, rt = _app(settings)
    sender, accounts = _multi_loan_sender(rt)
    _send(app, sender, "what is my emi")                                   # unverified -> loan chooser
    _send(app, sender, accounts[0], type="button")                         # picks a loan -> code emailed
    rt.channel.sent.clear()
    _send(app, sender, "and what's my interest rate?")                     # typed before the code
    assert any("I'll answer your message right after" in b for b in _bodies(rt))
    rt.channel.sent.clear()
    _send(app, sender, "424242")                                           # the code
    sent = _bodies(rt)
    assert any("Verified" in b for b in sent) and any("%" in b for b in sent)   # the kept question is answered


def test_hi_and_switch_loan_work_while_a_code_is_pending(settings):
    app, rt = _app(settings)
    sender, accounts = _multi_loan_sender(rt)
    _send(app, sender, "what is my emi")
    _send(app, sender, accounts[0], type="button")
    rt.channel.sent.clear()
    _send(app, sender, "hi")
    assert any(m["kind"] == "buttons" and "Welcome" in m["payload"]["interactive"]["body"]["text"] for m in rt.channel.sent)
    rt.channel.sent.clear()
    _send(app, sender, "switch loan")
    assert any("Which loan" in b for b in _bodies(rt))


def test_already_paid_from_a_reminder_asks_afresh_instead_of_an_old_other_loan_claim(settings):
    """16:05: 'Already paid' re-checked a 3:53 screenshot of the other loan. It should ask for the payment."""
    app, rt, sender, acc, client = _verified(settings)
    accounts = sorted({l["account_id"] for links in rt.store.links_by_account.values() for l in links
                       if rt.store.contacts[l["contact_id"]]["mobile"] == sender[-10:]})
    other = next(a for a in accounts if a != acc)
    p = next(x for x in rt.store.payments_by_account[other] if x["status"] == "success")
    _receipt(app, sender, {"utr": p["utr"], "amount": float(p["amount"]), "txn_date": p["txn_date"], "txn_time": None,
                           "payee_vpa": p["payee_vpa"]})
    assert any("linked to your other loan" in b for b in _bodies(rt))
    client.post(f"/admin/api/reminders/{acc}")
    rt.channel.sent.clear()
    _send(app, sender, "REMIND_PAID", type="button")
    sent = _bodies(rt)
    assert not any("linked to your other loan" in b for b in sent)
    assert any("UTR" in b and "screenshot" in b for b in sent)


def test_reminder_from_a_conversation_goes_to_that_number(settings):
    """16:22: the reminder for a loan with two numbers went to the other number that had chatted before."""
    app, rt, sender, acc, client = _verified(settings)
    links = rt.store.links_by_account[acc]
    others = [rt.store.contacts[l["contact_id"]]["mobile"] for l in links
              if rt.store.contacts[l["contact_id"]]["is_active"] and rt.store.contacts[l["contact_id"]]["mobile"] != sender[-10:]]
    for m in others:                                                        # another number on the loan chatted too
        rt.conversations.load(f"wa:{m}")
    cid = rt.conversation_id_for(sender)
    r = client.post(f"/admin/api/reminders/{acc}?conversation_id={cid}").json()
    assert r["conversation_id"] == cid and rt.channel.sent[-1]["to"].endswith(sender[-10:])
    bad = client.post(f"/admin/api/reminders/{acc}?conversation_id=wa:9000000000")
    assert bad.status_code == 409


def _logged_out(settings):
    app, rt, sender, acc, client = _verified(settings)
    cid = rt.conversation_id_for(sender)
    rt.end_session(cid, "logout")                                          # the borrower isn't logged in
    loans = client.get(f"/admin/api/conversations/{cid}/loans").json()     # the console's picker
    assert acc in [l["account_id"] for l in loans]
    r = client.post(f"/admin/api/reminders/{acc}?conversation_id={cid}")
    assert r.status_code == 200 and rt.channel.sent[-1]["kind"] == "buttons"
    rt.channel.sent.clear()
    return app, rt, sender, acc, client


def test_happy_flow_reminder_before_login_already_paid(settings):
    app, rt, sender, acc, client = _logged_out(settings)
    _send(app, sender, "REMIND_PAID", type="button")
    assert any("emailed a verification code" in b for b in _bodies(rt))       # code for that loan, no account no.
    rt.channel.sent.clear()
    _send(app, sender, "424242")
    sent = [b for b in _bodies(rt) if b]
    i_ver = next(i for i, b in enumerate(sent) if "Verified" in b)
    i_res = next(i for i, b in enumerate(sent) if "About the reminder for ₹" in b and acc[-4:] in b)
    i_ask = next(i for i, b in enumerate(sent) if "UTR" in b and "screenshot" in b)
    assert i_ver < i_res < i_ask                                              # verified -> nudge -> ask for proof


def test_happy_flow_reminder_before_login_cant_pay(settings):
    app, rt, sender, acc, client = _logged_out(settings)
    _send(app, sender, "REMIND_CANT", type="button")
    _send(app, sender, "424242")
    sent = [b for b in _bodies(rt) if b]
    assert any("About the reminder for ₹" in b for b in sent)
    assert any("Can't pay" not in b and ("pay" in b.lower() and "amount" in b.lower()) for b in sent)


def test_happy_flow_reminder_before_login_pay_now(settings):
    app, rt, sender, acc, client = _logged_out(settings)
    _send(app, sender, "REMIND_PAY", type="button")
    assert any("You can pay ₹" in b and "/pay/" in b for b in _bodies(rt))
    assert not any("verification code" in b for b in _bodies(rt))


def test_demo_lock_only_listed_numbers_get_reminders(settings):
    from dataclasses import replace
    app, rt, sender, acc, client = _verified(settings)
    cid = rt.conversation_id_for(sender)
    rt.settings = replace(rt.settings, demo_reminder_numbers="9000000000")      # someone else's number only
    assert client.post(f"/admin/api/reminders/{acc}?conversation_id={cid}").status_code == 409
    rt.settings = replace(rt.settings, demo_reminder_numbers=sender[-10:])
    assert client.post(f"/admin/api/reminders/{acc}?conversation_id={cid}").status_code == 200


def test_the_16_35_case_edited_receipt_on_a_confirmed_utr_is_double_checked_then_escalated(settings):
    """UTR ••••5432 was genuinely paid ₹1,481.99 (sent 3 times before). An edited ₹4,481.99 copy hit the resend limit
    and quoted an unrelated ticket. Now: double-check with the borrower, then a compliance ticket."""
    app, rt, sender, acc, client = _verified(settings)
    p = next(x for x in rt.store.payments_by_account[acc] if x["status"] == "success")
    genuine = {"utr": p["utr"], "amount": float(p["amount"]), "txn_date": p["txn_date"], "txn_time": None,
               "payee_vpa": p["payee_vpa"]}
    for _ in range(3):
        _receipt(app, sender, genuine)                                       # the real receipt, several times
    rt.channel.sent.clear()
    _receipt(app, sender, {**genuine, "amount": float(p["amount"]) + 3000})  # same UTR, edited amount
    sent = _bodies(rt)
    assert any("Is this the right screenshot?" in b and p["utr"][-4:] in b for b in sent)
    assert not any("several times" in b or "reference" in b.lower() for b in sent)        # no limit, no ticket ref
    rt.channel.sent.clear()
    _send(app, sender, "yes")
    t = next(t for t in rt.tools.tickets.list() if t["request_type"] == "payment_mismatch")
    assert t["queue"] == "compliance_review" and t["summary"]["category"] == "suspected_fake"
    assert t["summary"]["borrower_confirmed_screenshot"] is True
    assert any("We'll check this payment" in b for b in _bodies(rt)) and not any(t["ticket_id"] in b for b in _bodies(rt))


def test_no_its_not_the_right_screenshot_raises_nothing(settings):
    app, rt, sender, acc, client = _verified(settings)
    p = next(x for x in rt.store.payments_by_account[acc] if x["status"] == "success")
    _receipt(app, sender, {"utr": p["utr"], "amount": float(p["amount"]) + 3000, "txn_date": p["txn_date"],
                           "txn_time": None, "payee_vpa": p["payee_vpa"]})
    rt.channel.sent.clear()
    _send(app, sender, "no")
    assert any("Please share the correct receipt" in b for b in _bodies(rt))
    assert not [t for t in rt.tools.tickets.list() if t["request_type"] == "payment_mismatch"]


# ---------------- REM-02: the reminder's loan is the conversation's loan

def _other_loan_payment(rt, sender, acc):
    accounts = sorted({l["account_id"] for links in rt.store.links_by_account.values() for l in links
                       if rt.store.contacts[l["contact_id"]]["mobile"] == sender[-10:]})
    other = next(a for a in accounts if a != acc)
    p = next(x for x in rt.store.payments_by_account[other] if x["status"] == "success")
    return other, {"utr": p["utr"], "amount": float(p["amount"]), "txn_date": p["txn_date"], "txn_time": None,
                   "payee_vpa": p["payee_vpa"]}


def test_after_a_reminder_a_question_verifies_for_the_reminders_loan_without_a_chooser(settings):
    app, rt, sender, acc, client = _logged_out(settings)
    _send(app, sender, "what is my emi")
    sent = _bodies(rt)
    assert not any("Which loan is this about?" in b for b in sent)
    assert any("emailed a verification code" in b for b in sent)
    _send(app, sender, "424242")
    assert rt.conversations.load(rt.conversation_id_for(sender))["session"]["scope"]["account_id"] == acc


def test_after_a_reminder_the_menu_verifies_for_the_reminders_loan(settings):
    app, rt, sender, acc, client = _logged_out(settings)
    _send(app, sender, "hi")
    from servicing_agent.platform.inbound import MENU
    _send(app, sender, MENU[0]["id"], type="button")
    assert not any("Which loan is this about?" in b for b in _bodies(rt))
    _send(app, sender, "424242")
    assert rt.conversations.load(rt.conversation_id_for(sender))["session"]["scope"]["account_id"] == acc


def test_payment_on_the_other_loan_after_a_reminder_is_not_offered_a_switch(settings):
    """17:01: the reminder was for ...2619, the receipt's UTR is on ...5532. Stay on ...2619: say where the payment
    went, keep the reminder's amount due with a link, ask for this loan's receipt."""
    app, rt, sender, acc, client = _logged_out(settings)
    other, pay = _other_loan_payment(rt, sender, acc)
    _send(app, sender, "REMIND_PAID", type="button")
    _send(app, sender, "424242")
    rt.channel.sent.clear()
    _receipt(app, sender, pay)
    sent = _bodies(rt)
    assert not any("SWITCH LOAN" in b for b in sent)
    msg = next(b for b in sent if "made to your other loan" in b)
    assert other[-4:] in msg and acc[-4:] in msg and "/pay/" in msg and "the loan we reminded you about" in msg


def test_explicit_switch_still_shows_the_chooser_after_a_reminder(settings):
    app, rt, sender, acc, client = _logged_out(settings)
    _send(app, sender, "switch loan")
    assert any("Which loan is this about?" in b for b in _bodies(rt))


def test_without_a_reminder_the_other_loan_payment_still_offers_the_switch(settings):
    app, rt, sender, acc, client = _verified(settings)
    other, pay = _other_loan_payment(rt, sender, acc)
    _receipt(app, sender, pay)
    assert any("SWITCH LOAN" in b for b in _bodies(rt))
