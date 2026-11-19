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
    "pay_now": {"en": "You can pay ₹{amount} securely here: {payment_link}"},
    "receipt_sent": {"en": "Here is your receipt for ₹{amount} paid on {payment_date} (UTR {utr}). The PDF password is your date of birth as DDMMYYYY."},
    "statement_sent": {"en": "Here is your statement from {period_from} to {period_to}. The PDF password is your date of birth as DDMMYYYY."},
    "claim_matched": {"en": "Thank you. We've confirmed your payment of ₹{amount} on {payment_date} (UTR {utr}). Your receipt is attached."},
    "claim_pending": {"en": "Thanks. Payments can take up to a day to reflect. We'll confirm here once it's received."},
    "claim_review": {"en": "Thanks. We couldn't match this payment to your loan yet. Our team is reviewing it and will update you here."},
    "claim_need_utr": {"en": "Please share the 12-digit UTR / reference number of your payment, or a screenshot of the payment receipt."},
    "faq": {"en": "You can reach our customer care at {helpline}, Monday to Saturday, 9am to 6pm."},
    "routed": {"en": "I've passed this to our {team} team with the details of your message. They will get back to you here. Reference: {ticket_id}."},
    "handoff": {"en": "I'm connecting you with a member of our team right away. Reference: {ticket_id}. If you need urgent help, please call {helpline}."},
    "waiver_created": {"en": "Your request has been submitted for review. Reference: {case_id}. Our team reviews every request and no outcome has been decided yet. We'll update you here."},
    "waiver_duplicate": {"en": "Your request {case_id} is already under review. We'll update you here."},
    "waiver_confirm_change": {
        "en": "You asked for {old_desc} earlier. Do you want to change it to {new_desc}? This restarts the review and cancels the earlier request.",
    },
    "waiver_restarted": {"en": "Done. Your updated request has been submitted for review. Reference: {case_id}. The earlier request is cancelled."},
    "waiver_change_kept": {"en": "OK, we'll keep your earlier request {case_id} as it is."},
    "case_status": {"en": "Your request {case_id} is {status_text}. We'll update you here."},
    "no_open_case": {"en": "I can't find an open request on your loan."},
    "clarify": {"en": "Sorry, I didn't get that. Is this about a payment you made, a receipt or statement, your dues, or something else?"},
    "safe_fallback": {"en": "Sorry, I can't share that here. Is there anything else I can help you with on your loan?"},
}

STATUS_TEXT = {
    "PENDING_APPROVER": "under review",
    "PENDING_COMPLIANCE": "under review",
    "APPROVED": "under review",       # outcome is only communicated by a compliance template
    "REJECTED": "closed; our team will contact you with the details",
    "COMMUNICATED": "closed",
    "SUPERSEDED": "replaced by a newer request",
}

TEAM_NAMES = {
    "servicing_disputes": "disputes", "complaints": "customer care", "servicing_charges": "charges",
    "waiver_approvals": "approvals", "settlement_approvals": "approvals", "payment_reconciliation": "payments",
    "fraud_review": "payments", "priority_care": "priority care", "legal": "legal", "grievance_officer": "grievance",
    "security": "security", "servicing_general": "servicing",
}


def money(x: float | None) -> str:
    return f"{float(x or 0):,.2f}"


def render(template_id: str, lang: str = "en", buttons: list[str] | None = None,
           attachments: list[str] | None = None, **vars) -> Reply:
    tpl = T[template_id].get(lang) or T[template_id]["en"]
    return Reply(text=tpl.format(**vars), template_id=template_id, buttons=buttons or [], attachments=attachments or [])
