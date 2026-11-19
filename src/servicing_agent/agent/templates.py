"""Localized reply templates. Facts come from tool results; templates carry them.

Waiver/settlement/charge outcomes are template-only (no LLM free text). Outcome
templates sent after a decision must be signed off by compliance.
TODO(T1): add kn, ta, te, mr, bn. Missing languages fall back to English.
"""

from __future__ import annotations

from servicing_agent.contracts.state import Reply

T: dict[str, dict[str, str]] = {
    "greeting": {
        "en": "Hello! I can help with payments, receipts, statements, foreclosure amounts and requests about your loan. What do you need?",
        "hi": "नमस्ते! मैं भुगतान, रसीद, स्टेटमेंट और आपके लोन से जुड़े अनुरोधों में मदद कर सकता हूँ। आपको क्या चाहिए?",
    },
    "ask_account": {
        "en": "To help with this, please share your loan account number.",
        "hi": "इसमें मदद के लिए कृपया अपना लोन अकाउंट नंबर भेजें।",
    },
    "invalid_account_format": {"en": "That doesn't look like a valid account number. Please check and send it again."},
    "otp_sent_neutral": {
        "en": "If the details match our records, you'll receive an OTP by SMS. Please reply with the 6-digit code.",
        "hi": "अगर जानकारी हमारे रिकॉर्ड से मेल खाती है, तो आपको SMS पर OTP मिलेगा। कृपया 6 अंकों का कोड भेजें।",
    },
    "otp_prompt": {"en": "Please reply with the 6-digit OTP you received by SMS."},
    "otp_retry": {"en": "That code didn't work. Please check the SMS and try again."},
    "verify_failed": {"en": "We couldn't verify these details. Please message us from your registered mobile number or call {helpline}."},
    "locked": {"en": "For your security, verification is paused for a while. Please try again later or call {helpline}."},
    "verified_ok": {"en": "Thanks, you're verified."},
    "other_account_notice": {"en": "I can only help with the loan you verified. To ask about another loan, share its account number and we'll verify it."},
    "foreclosure_quote": {"en": "Your foreclosure amount is ₹{amount}, valid until {valid_until}. Breakup: principal ₹{principal}, overdue ₹{overdue}, charges ₹{charges}, foreclosure fee ₹{foreclosure_fee}."},
    "emi_due": {"en": "Your EMI is ₹{emi_amount}, next due on {next_due_date}. Overdue amount: ₹{overdue_amount}."},
    "payment_history": {"en": "Your last {count} payments:\n{lines}"},
    "payment_history_empty": {"en": "We don't have any payments recorded on your loan in the last {months} months."},
    "loan_summary": {"en": "Your {product} loan: principal outstanding ₹{principal}, interest rate {rate}% p.a., "
                           "EMI ₹{emi}, {tenure_left} left, next due on {next_due_date}. Overdue: ₹{overdue}."},
    "loan_rate": {"en": "The interest rate on your {product} loan is {rate}% p.a."},
    "loan_outstanding": {"en": "Your principal outstanding is ₹{principal}, and the overdue amount is ₹{overdue}."},
    "loan_emis_left": {"en": "You have {emis_left} of ₹{emi} left; the next one is due on {next_due_date}."},
    "charges": {"en": "Charges on your loan: bounce charges ₹{bounce}, late-payment (penal) charges ₹{penal}, "
                      "total ₹{total}. A bounce charge applies when an EMI payment fails; penal charges apply "
                      "to amounts that are overdue. If you think a charge is wrong, tell us and we'll pass it "
                      "to our team to review."},
    "charges_none": {"en": "There are no bounce or late-payment charges on your loan right now."},
    "pay_now": {"en": "You can pay ₹{amount} securely here: {payment_link}"},
    "receipt_sent": {"en": "Here is your receipt for ₹{amount} paid on {payment_date} (UTR {utr}). The PDF password is your date of birth as DDMMYYYY."},
    "statement_sent": {"en": "Here is your statement from {period_from} to {period_to}. The PDF password is your date of birth as DDMMYYYY."},
    "claim_matched": {"en": "Thank you. We've confirmed your payment of ₹{amount} on {payment_date} (UTR {utr}). Your receipt is attached."},
    "claim_already_confirmed": {"en": "This payment of ₹{amount} on {payment_date} (UTR {utr}) is already confirmed. Here is your receipt again."},
    "claim_review_existing": {"en": "We're already reviewing this payment. Reference: {ticket_id}. We'll update you here."},
    "claim_pending": {"en": "Thanks. Payments can take up to a day to reflect. We'll confirm here once it's received."},
    "claim_review": {"en": "Thanks. We couldn't match this payment to your loan yet. Our team is reviewing it and will update you here."},
    "claim_checking": {"en": "Thanks, we've received your payment proof{detail}. It isn't showing in our records yet, so "
                             "I'll check again in {window} and confirm here."},
    "claim_failed_escalated": {"en": "Update on your payment{detail}: it still isn't showing in our records after {window}, "
                                     "so we couldn't confirm it. We've filed an escalation with our payments team to check "
                                     "it with the bank. Reference: {ticket_id}. We'll update you here. Please don't pay "
                                     "the same amount again in the meantime."},
    "claim_escalated_notice": {"en": "We couldn't confirm this payment{detail}, so we've passed it to our team to check. "
                                     "Reference: {ticket_id}. We'll update you here."},
    "claim_unverified": {"en": "Thanks. We couldn't verify this payment from the receipt you shared. If you have another "
                               "receipt, or the 12-digit UTR from your bank or UPI app, please send it here."},
    # {consequences}: the borrower lines of the policy clauses retrieved for this request (servicing_policy.md).
    "case_confirm": {"en": "You're asking for {desc}.\nPlease note before I submit:\n{consequences}\n"
                           "Shall I submit the request? Reply YES to submit or NO to cancel."},
    "case_confirm_partial": {"en": "Thanks, your payment of ₹{paid} is confirmed. Your total due is ₹{due}, so ₹{shortfall} "
                                   "is still outstanding, and you're asking for {desc}.\nPlease note before I submit:\n"
                                   "{consequences}\nShall I submit the request? Reply YES to submit or NO to cancel."},
    "policy_answer": {"en": "{answer}\nIf you'd like, I can help with your request or check your loan details."},
    "case_not_needed": {"en": "Your payment of ₹{paid} covers the full amount due (₹{due}), so there's nothing left to "
                              "{verb} and no request is needed."},
    "case_waits_for_payment": {"en": "We need to confirm the payment you shared before we can look at your {kind} request. "
                                     "Once it's confirmed, send the request again and I'll take it forward."},
    "case_cancelled": {"en": "Okay, I haven't submitted anything. If you change your mind, just tell me."},
    "claim_other_own_loan": {"en": "This payment{detail} is linked to your other loan {other}, not the loan you're "
                                   "verified for right now ({current}). Reply SWITCH LOAN to verify that loan and I'll "
                                   "check the payment there."},
    "claim_latest_confirmed": {"en": "Your payment{detail} is already confirmed. You don't need to do anything else."},
    "claim_latest_other_loan": {"en": "The payment you shared{detail} is linked to your other loan. Reply SWITCH LOAN to "
                                      "verify that loan and I'll check it there."},
    "claim_latest_unverified": {"en": "I understand you've made the payment. We couldn't verify it from the receipt you "
                                      "shared{detail}. Please send the 12-digit UTR from your bank or UPI app, or another "
                                      "receipt, and I'll check it right away. You can also call us at {helpline}."},
    "claim_still_checking": {"en": "We're still checking your payment{detail} and will update you here within {window}."},
    "context_payment_recall": {"en": "The last payment you shared here was ₹{amount} (UTR ending {utr4}, dated {date}).{status}"},
    "claim_need_utr": {"en": "Please share the 12-digit UTR / reference number of your payment, or a screenshot of the payment receipt."},
    "faq": {"en": "You can reach our customer care at {helpline}, Monday to Saturday, 9am to 6pm."},
    "routed": {"en": "I've passed this to our {team} team with the details of your message. They will get back to you here. Reference: {ticket_id}."},
    "handoff": {"en": "I'm connecting you with a member of our team right away. Reference: {ticket_id}. If you need urgent help, please call {helpline}."},
    "waiver_created": {"en": "Submitted. Your {kind} request is now with a supervisor for review (step 1 of 2), then our "
                             "compliance team (step 2 of 2). Reference: {case_id}. Nothing has been decided yet; we'll "
                             "message you the outcome here. Send \"status\" any time for an update."},
    "hardship_ack": {"en": "I'm sorry you're going through a difficult time. I've marked your request as a priority "
                           "for our team."},
    "waiver_duplicate": {"en": "Your request {case_id} is already under review. We'll update you here."},
    "waiver_confirm_change": {
        "en": "You asked for {old_desc} earlier. Do you want to change it to {new_desc}? This restarts the review and cancels the earlier request.",
    },
    "waiver_restarted": {"en": "Done. Your updated request has been submitted for review. Reference: {case_id}. The earlier request is cancelled. It goes through the same steps: a supervisor reviews it first, then our compliance team, and we'll message you here with the outcome."},
    "waiver_change_kept": {"en": "OK, we'll keep your earlier request {case_id} as it is."},
    "case_status": {"en": "Your request {case_id} is {status_text}. We'll update you here."},
    "no_open_case": {"en": "I can't find an open request on your loan."},
    "clarify": {"en": "Sorry, I didn't quite get that. You can ask things like \"what is my EMI?\", \"interest rate\", "
                      "\"when does my loan end?\", \"send my statement\", or share a payment screenshot."},
    "account_facts": {"en": "Here are your {product} loan details: loan amount ₹{sanction}, principal outstanding ₹{principal}, "
                            "interest rate {rate}% p.a., EMI ₹{emi}, tenure {tenure_months} months with {tenure_left} EMIs left, "
                            "next due on {next_due_date}, overdue ₹{overdue}. For anything else, call us at {helpline}."},
    "restricted_info": {"en": "Sorry, I can't share internal details such as collection agent or agency assignments or credit "
                              "bureau information here. I can help with your dues, payments, receipts, statements, interest "
                              "rate or loan details. If you have a concern about how someone contacted you, tell me and "
                              "I'll pass it to our team."},
    "out_of_scope": {"en": "I can only help with your loan here: payments, dues, receipts, statements, interest rate, "
                           "foreclosure or requests about your account. What would you like to know?"},
    "safe_fallback": {"en": "Sorry, I can't share that here. Is there anything else I can help you with on your loan?"},
}

STATUS_TEXT = {
    "PENDING_APPROVER": "with a supervisor for review (step 1 of 2)",
    "PENDING_COMPLIANCE": "with our compliance team for a final check (step 2 of 2)",
    "APPROVED": "under review",       # outcome is only communicated by a compliance template
    "REJECTED": "closed; our team will contact you with the details",
    "COMMUNICATED": "closed",
    "SUPERSEDED": "replaced by a newer request",
}

TEAM_NAMES = {
    "servicing_disputes": "disputes", "complaints": "customer care", "servicing_charges": "charges",
    "waiver_approvals": "approvals", "settlement_approvals": "approvals", "payment_reconciliation": "payments",
    "fraud_review": "payments", "priority_care": "priority care", "legal": "legal", "grievance_officer": "grievance",
    "security": "security", "servicing_general": "servicing", "compliance_review": "compliance",
}


def money(x: float | None) -> str:
    return f"{float(x or 0):,.2f}"


def render(template_id: str, lang: str = "en", buttons: list[str] | None = None,
           attachments: list[str] | None = None, **vars) -> Reply:
    tpl = T[template_id].get(lang) or T[template_id]["en"]
    return Reply(text=tpl.format(**vars), template_id=template_id, buttons=buttons or [], attachments=attachments or [])
