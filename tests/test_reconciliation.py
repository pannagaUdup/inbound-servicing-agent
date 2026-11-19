from datetime import date, timedelta

from servicing_agent.agent.tools.reconciliation import Verdict, reconcile


def _txn(runtime, borrower):
    a, _ = borrower
    return a, next(p for p in runtime.store.payments_by_account[a["account_id"]] if p["status"] == "success")


def test_exact_match(runtime, borrower):
    a, p = _txn(runtime, borrower)
    res = reconcile(runtime.tools.payments, a["account_id"], p["utr"], p["amount"], p["txn_date"])
    assert res.verdict == Verdict.MATCHED


def test_edited_amount_is_flagged(runtime, borrower):
    a, p = _txn(runtime, borrower)
    res = reconcile(runtime.tools.payments, a["account_id"], p["utr"], p["amount"] + 1000, p["txn_date"])
    assert res.verdict == Verdict.MISMATCH and not res.accepted


def test_other_accounts_utr_is_flagged(runtime, borrower):
    a, _ = borrower
    other = next(p for p in runtime.store.payments if p["account_id"] and p["account_id"] != a["account_id"])
    res = reconcile(runtime.tools.payments, a["account_id"], other["utr"], other["amount"], other["txn_date"])
    assert res.verdict == Verdict.OTHER_ACCOUNT


def _confirm(runtime, account_id, p):
    runtime.claims.record(conversation_id="c", account_id=account_id, utr=p["utr"], result="MATCHED",
                          source="screenshot", message_id="m1")


def test_resend_by_same_borrower_is_already_confirmed_not_fraud(runtime, borrower):
    a, p = _txn(runtime, borrower)
    _confirm(runtime, a["account_id"], p)
    res = reconcile(runtime.tools.payments, a["account_id"], p["utr"], p["amount"], p["txn_date"])
    assert res.verdict == Verdict.ALREADY_CONFIRMED and res.txn["utr"] == p["utr"]


def test_edited_copy_of_confirmed_receipt_is_still_flagged(runtime, borrower):
    a, p = _txn(runtime, borrower)
    _confirm(runtime, a["account_id"], p)
    res = reconcile(runtime.tools.payments, a["account_id"], p["utr"], p["amount"] * 2, p["txn_date"])
    assert res.verdict == Verdict.MISMATCH                 # not "already confirmed"


def test_utr_confirmed_for_someone_else_is_replay(runtime, borrower):
    a, p = _txn(runtime, borrower)
    _confirm(runtime, "CN0000000000", p)                 # ledger says another account already used it
    res = reconcile(runtime.tools.payments, a["account_id"], p["utr"], p["amount"], p["txn_date"])
    assert res.verdict == Verdict.REPLAY


def test_unknown_utr_inside_window_is_pending_and_outside_is_not_found(runtime, borrower):
    a, _ = borrower
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=10)).isoformat()
    assert reconcile(runtime.tools.payments, a["account_id"], "123412341234", 500, today).verdict == Verdict.PENDING
    assert reconcile(runtime.tools.payments, a["account_id"], "123412341234", 500, old).verdict == Verdict.NOT_FOUND


def test_future_date_and_foreign_payee(runtime, borrower):
    a, p = _txn(runtime, borrower)
    future = (date.today() + timedelta(days=3)).isoformat()
    assert reconcile(runtime.tools.payments, a["account_id"], p["utr"], p["amount"], future).verdict == Verdict.NOT_FOUND
    assert reconcile(runtime.tools.payments, a["account_id"], p["utr"], p["amount"], p["txn_date"],
                     payee_vpa="someone@ybl").verdict == Verdict.MISMATCH


def test_masked_vpa_matching():
    from servicing_agent.agent.tools.reconciliation import payee_matches
    ours = "collect@cnbank"
    assert payee_matches("collect@cnbank", ours)
    assert payee_matches("COLLECT@CNBANK", ours)
    assert payee_matches("....ect@cnbank", ours)        # Google Pay style prefix mask
    assert payee_matches("••••@cnbank", ours)
    assert payee_matches("col***@cnbank", ours)          # middle mask
    assert payee_matches("....", ours)                   # nothing visible: D7 decides
    assert not payee_matches("....arvy@ptys", ours)      # visible part is someone else's
    assert not payee_matches("someone@ybl", ours)
    assert not payee_matches("john.doe@cnbank", ours)    # single dot is not a mask


def test_genuine_payment_with_masked_payee_is_matched(runtime, borrower):
    a, p = _txn(runtime, borrower)
    res = reconcile(runtime.tools.payments, a["account_id"], p["utr"], p["amount"], p["txn_date"],
                    payee_vpa="....ect@cnbank")
    assert res.verdict == Verdict.MATCHED


def test_ocr_tolerance_single_digit(runtime, borrower):
    a, p = _txn(runtime, borrower)
    misread = p["utr"][:-1] + str((int(p["utr"][-1]) + 1) % 10)
    res = reconcile(runtime.tools.payments, a["account_id"], misread, p["amount"], p["txn_date"])
    assert res.verdict == Verdict.MATCHED
