"""Verification: account number + sender's registered mobile (+ OTP step-up).

* The account number is format-checked (CN + 10 digits, Luhn) before any lookup.
* Match = account exists AND the sender's normalised number is an active contact
  linked to that account. A mobile number is never a lookup key on its own.
* The reply is identical whether or not the details match; the borrower never
  learns which factor failed.
* One account per session. Sending a different account number re-verifies and
  replaces the scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from servicing_agent.agent.templates import render
from servicing_agent.config import Settings
from servicing_agent.contracts.events import EventType
from servicing_agent.contracts.state import AuthScope, Reply, VerificationState
from servicing_agent.data.identifiers import OTP_RE, extract_account_number, is_valid_account_number, normalize_mobile

SESSION_TTL = timedelta(minutes=30)
LOCK_FOR = timedelta(minutes=30)


@dataclass
class VerifyOutcome:
    state: VerificationState
    scope: AuthScope | None
    replies: list[Reply] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    just_verified: bool = False
    security_alert: bool = False


def looks_like_account_attempt(text: str) -> bool:
    t = (text or "").strip().upper()
    return bool(extract_account_number(t)) or t.startswith("CN")


def handle(text: str, sender: str, state: VerificationState, scope: AuthScope | None,
           tools, settings: Settings, lang: str = "en", now: datetime | None = None) -> VerifyOutcome:
    now = now or datetime.utcnow()
    st = state.model_copy(deep=True)
    out = VerifyOutcome(state=st, scope=scope)

    def ev(t: EventType, **data):
        out.events.append({"type": t.value, "data": data})

    if st.stage == "locked":
        if st.locked_until and st.locked_until > now:
            out.replies.append(render("locked", lang, helpline=settings.helpline))
            return out
        st.stage, st.otp_attempts = "awaiting_account", 0

    account = extract_account_number(text)
    raw_attempt = looks_like_account_attempt(text)

    if account or (raw_attempt and st.stage in ("none", "awaiting_account")):
        if not account or not is_valid_account_number(account):
            st.stage = "awaiting_account"
            st.failed_attempts += 1
            ev(EventType.VERIFICATION_FAILED, reason="bad_format")
            out.replies.append(render("invalid_account_format", lang))
            return _maybe_lock(out, settings, now, lang)

        contact = tools.lms.active_contact_for(account, sender) if tools.lms.account_exists(account) else None
        ev(EventType.VERIFICATION_STARTED, matched=bool(contact))
        st.otp_attempts = 0
        if contact:
            st.candidate_account_id = account
            st.candidate_contact_id = contact["contact_id"]
            st.candidate_customer_id = contact["customer_id"]
            st.candidate_contact_added_on = contact["added_on"]
        else:
            st.candidate_account_id = st.candidate_contact_id = st.candidate_customer_id = None
            st.failed_attempts += 1
            ev(EventType.VERIFICATION_FAILED, reason="no_match")

        if not settings.otp_enabled:
            if contact:
                return _verified(out, st, "account+mobile", now, lang)
            out.replies.append(render("verify_failed", lang, helpline=settings.helpline))
            st.stage = "awaiting_account"
            return _maybe_lock(out, settings, now, lang)

        st.otp_request_id = tools.otp.send(normalize_mobile(sender)) if contact else None
        if contact:
            ev(EventType.VERIFICATION_OTP_SENT)
        st.stage = "awaiting_otp"
        out.scope = None if scope and scope.account_id != account else scope
        out.replies.append(render("otp_sent_neutral", lang))
        return _maybe_lock(out, settings, now, lang)

    if st.stage == "awaiting_otp":
        m = OTP_RE.search(text or "")
        if not m:
            out.replies.append(render("otp_prompt", lang))
            return out
        ok = bool(st.candidate_account_id and st.otp_request_id and tools.otp.verify(st.otp_request_id, m.group(0)))
        if ok:
            return _verified(out, st, "account+mobile+otp", now, lang)
        st.otp_attempts += 1
        ev(EventType.VERIFICATION_FAILED, reason="otp", attempt=st.otp_attempts)
        if st.otp_attempts >= settings.max_otp_attempts:
            st.stage, st.locked_until = "locked", now + LOCK_FOR
            st.candidate_account_id = st.otp_request_id = None
            out.replies.append(render("verify_failed", lang, helpline=settings.helpline))
        else:
            out.replies.append(render("otp_retry", lang))
        return out

    # awaiting_account but the message isn't an account number
    st.stage = "awaiting_account"
    out.replies.append(render("ask_account", lang))
    return out


def _verified(out: VerifyOutcome, st: VerificationState, method: str, now: datetime, lang: str) -> VerifyOutcome:
    out.scope = AuthScope(account_id=st.candidate_account_id, customer_id=st.candidate_customer_id,
                          contact_id=st.candidate_contact_id, method=method, verified_at=now,
                          expires_at=now + SESSION_TTL)
    recent_contact = bool(st.candidate_contact_added_on and
                          date.fromisoformat(st.candidate_contact_added_on) >= now.date() - timedelta(days=7))
    out.events.append({"type": EventType.VERIFIED.value,
                       "data": {"method": method, "account_id": st.candidate_account_id,
                                "recent_contact_change": recent_contact}})
    st.stage, st.otp_attempts, st.otp_request_id = "verified", 0, None
    out.just_verified = True
    return out


def _maybe_lock(out: VerifyOutcome, settings: Settings, now: datetime, lang: str) -> VerifyOutcome:
    if out.state.failed_attempts >= settings.max_failed_verifications_per_sender_24h:
        out.state.stage, out.state.locked_until = "locked", now + timedelta(hours=24)
        out.security_alert = True
        out.events.append({"type": EventType.SECURITY_ALERT.value, "data": {"reason": "repeated_failed_verification"}})
        out.replies = [render("locked", lang, helpline=settings.helpline)]
    return out


def scope_valid(scope: AuthScope | None, now: datetime | None = None) -> bool:
    return bool(scope and scope.expires_at > (now or datetime.utcnow()))
