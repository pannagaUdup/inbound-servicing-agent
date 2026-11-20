"""Resolving a ticket notifies the borrower — except for internal fraud/security types."""

from servicing_agent.platform.api.admin import _notify_ticket_resolved


def _texts(channel):
    out = []
    for m in channel.sent:
        body = (m.get("payload", {}).get("text") or {}).get("body")
        if body:
            out.append(body)
    return out


def test_resolving_a_ticket_notifies_the_borrower(runtime):
    tickets = runtime.tools.tickets
    cid = "wa:919990001111"
    t = tickets.create("dispute", {"demo": True}, cid, account_id=None)
    tid = t["ticket_id"]
    assert tickets.resolve(tid) is True
    _notify_ticket_resolved(runtime, tickets.get(tid))
    assert any(tid in x and "resolved" in x.lower() for x in _texts(runtime.channel))


def test_fraud_security_ticket_resolution_is_suppressed(runtime):
    tickets = runtime.tools.tickets
    before = len(runtime.channel.sent)
    t = tickets.create("payment_mismatch", {"demo": True}, "wa:919990002222")
    tickets.resolve(t["ticket_id"])
    _notify_ticket_resolved(runtime, tickets.get(t["ticket_id"]))
    assert len(runtime.channel.sent) == before   # no borrower message for an internal fraud ticket
