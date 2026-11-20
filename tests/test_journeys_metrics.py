"""Journeys metrics: inbound self-serve funnel (narrowing) + grouped 'what borrowers ask for'."""

from datetime import datetime

from servicing_agent.platform.db import AuditEvent


def test_inbound_funnel_and_grouped_intents(runtime):
    now = datetime.utcnow()
    with runtime.session_factory.begin() as s:
        s.add_all([
            # conv 1: verified, zero-touch, a payment intent
            AuditEvent(conversation_id="wa:j1", type="inbound", data={}, at=now),
            AuditEvent(conversation_id="wa:j1", type="verified", data={}, at=now),
            AuditEvent(conversation_id="wa:j1", type="tool_call", data={"intent": "pay_now"}, at=now),
            # conv 2: escalated (complaint)
            AuditEvent(conversation_id="wa:j2", type="inbound", data={}, at=now),
            AuditEvent(conversation_id="wa:j2", type="handoff", data={"intent": "complaint"}, at=now),
        ])
    j = runtime.metrics.journeys()
    f = j["inbound_funnel"]
    assert f["conversations"] == 2
    assert f["verified"] == 1
    assert f["escalated"] == 1
    assert f["zero_touch"] == 1                       # conv 1 only (conv 2 was handed off)
    assert f["zero_touch"] + f["escalated"] == f["conversations"]   # strictly narrowing funnel
    assert j["by_intent_grouped"].get("Payments") == 1
    assert j["by_intent_grouped"].get("Complaints & disputes") == 1
