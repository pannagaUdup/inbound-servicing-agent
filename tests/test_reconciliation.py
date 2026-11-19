from datetime import date, timedelta

from servicing_agent.data.reconciliation import Verdict, reconcile


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


def test_replay_is_flagged(runtime, borrower):
    a, p = _txn(runtime, borrower)
    runtime.tools.payments.mark_claimed(p["utr"], a["account_id"])
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


def test_ocr_tolerance_single_digit(runtime, borrower):
    a, p = _txn(runtime, borrower)
    misread = p["utr"][:-1] + str((int(p["utr"][-1]) + 1) % 10)
    res = reconcile(runtime.tools.payments, a["account_id"], misread, p["amount"], p["txn_date"])
    assert res.verdict == Verdict.MATCHED
