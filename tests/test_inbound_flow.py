"""Inbound menu flow (greeting -> menu -> account number -> email OTP) against the agreed constraints.

Constraints (docs/DECISIONS.md): account number + sender is an ACTIVE registered contact on that
account + OTP; a mobile number is never a lookup key on its own; nothing about any account before
the OTP; the same reply whether or not the details match; repeated failures are blocked.
"""

import json
import re
from dataclasses import replace

import pytest

from servicing_agent.contracts.state import InboundMessage
from servicing_agent.data.identifiers import make_account_number
from servicing_agent.platform.channel import MockChannel
from servicing_agent.platform.conversations import ConversationStore
from servicing_agent.platform.email import MockEmailSender
from servicing_agent.platform.inbound import InboundFlow


class FakeOtp:
    def __init__(self):
        self.issued: dict[str, str] = {}

    def issue(self, key):
        self.issued[key] = "424242"
        return "424242"

    def verify(self, key, code):
        if key not in self.issued:
            return ("expired", 0)
        return ("ok", 2) if code == self.issued[key] else ("wrong", 2)


@pytest.fixture
def flow(runtime, settings):
    channel, otp = MockChannel(), FakeOtp()
    f = InboundFlow(runtime.store, channel, MockEmailSender(), ConversationStore(),
                    replace(settings, otp_email_override=""), otp=otp)
    return f, channel, otp


def say(f, sender, text, kind="text"):
    return f.handle(InboundMessage(message_id=f"m-{text}", sender=sender, type=kind, text=text))


def texts(channel, sender=None):
    """Message bodies sent (text, buttons or list), without recipient metadata."""
    out = []
    for m in channel.sent:
        if sender is not None and m["to"] != sender:
            continue
        p = m["payload"]
        body = (p.get("text") or {}).get("body") or ((p.get("interactive") or {}).get("body") or {}).get("text")
        out.append(body if body is not None else json.dumps(p, ensure_ascii=False))
    return out


def other_account(runtime, account_id):
    return next(a for a in runtime.store.accounts if a != account_id)


def test_registered_contact_gets_otp_and_scope(flow, borrower):
    f, channel, otp = flow
    a, sender = borrower
    say(f, sender, "hi")
    r = say(f, sender, "PAY_INFO", kind="button")
    assert "loan account number" in texts(channel)[-1]               # always asks; never guesses from the mobile
    say(f, sender, a["account_id"])
    assert otp.issued                                                 # OTP issued only after the contact check
    r = say(f, sender, "424242")
    assert r["serve"] == ["show my payment history"]
    scope = f.conversations.load(f._cid(sender))["session"]["scope"]
    assert scope["account_id"] == a["account_id"] and scope["contact_id"]


def test_same_reply_and_no_otp_when_details_do_not_match(flow, borrower, runtime):
    f, channel, otp = flow
    a, sender = borrower
    say(f, sender, "hi"); say(f, sender, "GEN_SOA", kind="button")
    say(f, sender, a["account_id"])
    matched_reply = texts(channel)[-1]

    stranger = "919000000123"
    say(f, stranger, "hi"); say(f, stranger, "GEN_SOA", kind="button")
    say(f, stranger, a["account_id"])                                 # real account, unregistered number
    assert texts(channel, stranger)[-1] == matched_reply              # identical reply
    assert f._cid(stranger) not in otp.issued                         # but no OTP was sent
    for left in (2, 1):
        say(f, stranger, "424242")
        assert f"({left} left)" in texts(channel, stranger)[-1]       # looks like a wrong code
    say(f, stranger, "424242")
    assert "Too many incorrect attempts" in texts(channel, stranger)[-1]
    assert not f.conversations.load(f._cid(stranger))["session"].get("scope")


def test_unknown_account_gets_the_same_neutral_reply(flow, borrower):
    f, channel, _ = flow
    _, sender = borrower
    say(f, sender, "hi"); say(f, sender, "GEN_SOA", kind="button")
    say(f, sender, make_account_number("000000001"))                  # valid format, not a real loan
    assert texts(channel)[-1] == InboundFlow.NEUTRAL_OTP_MSG


def test_nothing_about_any_account_before_otp(flow, borrower, runtime):
    f, channel, _ = flow
    a, sender = borrower
    say(f, sender, "hi"); say(f, sender, "ACCOUNT_STATUS", kind="button"); say(f, sender, a["account_id"])
    pre_otp = " ".join(texts(channel))
    assert not any(m["kind"] == "list" for m in channel.sent)         # no loan chooser
    assert "₹" not in pre_otp and "overdue" not in pre_otp.lower()
    assert not re.search(r"CN\d{2}", pre_otp)                          # no account numbers, masked or not
    assert "@" not in pre_otp                                          # no (masked) email address


def test_shared_number_cannot_reach_another_customers_loan(flow, runtime):
    f, channel, otp = flow
    store = runtime.store
    by_mobile = {}
    for c in store.contacts.values():
        if c["is_active"]:
            by_mobile.setdefault(c["mobile"], set()).add(c["customer_id"])
    shared = next(((m, cs) for m, cs in by_mobile.items() if len(cs) > 1), None)
    if shared is None:
        pytest.skip("no shared number in this dataset")
    mobile, customers = shared
    sender = "91" + mobile
    # A loan belonging to a customer who does NOT have this number on that loan.
    target = next(acc for acc, links in store.links_by_account.items()
                  if f._active_contact(acc, sender) is None)
    say(f, sender, "hi"); say(f, sender, "GEN_SOA", kind="button"); say(f, sender, target)
    assert f._cid(sender) not in otp.issued


def test_repeated_failures_are_blocked(flow, borrower):
    f, channel, _ = flow
    _, sender = borrower
    say(f, sender, "hi"); say(f, sender, "GEN_SOA", kind="button")
    for i in range(InboundFlow.MAX_FAILURES_24H):
        f.conversations.load(f._cid(sender))["session"]["inbound"] = {"stage": "awaiting_account", "intent": "GEN_SOA"}
        say(f, sender, make_account_number(f"{i:09d}"))
    f.conversations.load(f._cid(sender))["session"]["inbound"] = {"stage": "awaiting_account", "intent": "GEN_SOA"}
    say(f, sender, make_account_number("000000099"))
    assert "paused" in texts(channel)[-1]


def test_verified_session_is_served_without_a_second_otp(flow, borrower):
    f, channel, otp = flow
    a, sender = borrower
    say(f, sender, "hi"); say(f, sender, "GEN_SOA", kind="button"); say(f, sender, a["account_id"]); say(f, sender, "424242")
    otp.issued.clear()
    say(f, sender, "hi")                                               # menu again
    r = say(f, sender, "PAY_INFO", kind="button")
    assert r["serve"] == ["show my payment history"] and not otp.issued
