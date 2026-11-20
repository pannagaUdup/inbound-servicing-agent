"""'logout' ends the session; ops can force it; 'which loan am I logged in to?' names the active loan."""

from fastapi.testclient import TestClient

from servicing_agent.agent.planner import RulePlanner
from servicing_agent.contracts.state import Intent
from tests.test_agent_flow import say, verify


def test_session_info_is_recognised():
    for t in ("which account am I logged into", "which loan is this", "what is my current account number",
              "am I still verified", "which loan is active"):
        assert [x.intent for x in RulePlanner().plan(t)][0] == Intent.SESSION_INFO, t


def test_which_loan_am_i_on(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "which account am I logged into?")
    t = r.replies[0].text
    assert r.replies[0].template_id == "session_info" and a["account_id"][-4:] in t and a["account_id"] not in t
    assert "IST" in t and "LOGOUT" in t and "SWITCH LOAN" in t


def test_which_loan_when_not_verified_does_not_start_verification(runtime, borrower):
    a, sender = borrower
    r = say(runtime, sender, "which account am I logged into?")
    assert r.replies[0].template_id == "session_info_none"


def test_logout_ends_the_session(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    r = say(runtime, sender, "logout")
    assert r.replies[0].template_id == "logged_out" and a["account_id"][-4:] in r.replies[0].text
    sess = runtime.conversations.load(runtime.conversation_id_for(sender))["session"]
    assert sess["scope"] is None and not sess.get("pending_action")
    assert say(runtime, sender, "which loan am I on").replies[0].template_id == "session_info_none"
    assert say(runtime, sender, "what is my emi").replies[0].template_id == "ask_account"     # must verify again


def test_logout_drops_a_pending_question(runtime, borrower):
    a, sender = borrower
    verify(runtime, a, sender)
    say(runtime, sender, "please waive my penal charges of Rs 5000, my salary was delayed")   # YES / NO pending
    say(runtime, sender, "log out")
    verify(runtime, a, sender)
    assert say(runtime, sender, "yes").replies[0].template_id != "waiver_created"


def test_ops_can_force_logout_from_the_console(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    app = create_app(settings)
    client = TestClient(app)
    rt = app.state.runtime
    a, sender = borrower
    verify(rt, a, sender)
    cid = rt.conversation_id_for(sender)
    row = next(c for c in client.get("/admin/api/conversations").json() if c["conversation_id"] == cid)
    assert row["account_id"] == a["account_id"] and row["session_expires_at"]
    r = client.post(f"/admin/api/conversations/{cid}/logout").json()
    assert r["ended"] and r["account_id"] == a["account_id"] and r["notified"]
    assert "ended by our team" in rt.channel.sent[-1]["payload"]["text"]["body"] if hasattr(rt.channel, "sent") else True
    assert rt.conversations.load(cid)["session"]["scope"] is None
    assert any(e["type"] == "session_ended" and e["data"]["reason"] == "forced by ops"
               for e in rt.conversations.load(cid)["events"])
    assert client.post(f"/admin/api/conversations/{cid}/logout").json()["ended"] is False       # nothing left to end
