"""Per-node tracing (SA_TRACING) and the Traces console API.

The key property: tracing never changes what the agent does or says.
"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from servicing_agent.agent.graph import new_message_id
from servicing_agent.contracts.cases import CaseRequest, CaseType, DecisionIn
from servicing_agent.contracts.state import InboundMessage
from servicing_agent.platform.db import TraceSpan
from servicing_agent.platform.runtime import build_runtime


def say(runtime, sender, text=None):
    return runtime.run_turn(InboundMessage(message_id=new_message_id(), sender=sender, text=text))


@pytest.fixture
def traced(settings):
    return build_runtime(replace(settings, tracing=True))


def _spans(rt, **where):
    with rt.session_factory() as s:
        q = select(TraceSpan).order_by(TraceSpan.id)
        for k, v in where.items():
            q = q.where(getattr(TraceSpan, k) == v)
        return s.execute(q).scalars().all()


def test_flag_off_writes_nothing(settings, borrower):
    _, sender = borrower
    runtime = build_runtime(replace(settings, tracing=False))
    say(runtime, sender, "emi due?")
    assert runtime.trace_sink is None and _spans(runtime) == []


def test_turn_writes_node_spans_and_root(traced, borrower):
    a, sender = borrower
    say(traced, sender, "what is my foreclosure amount")
    say(traced, sender, a["account_id"])
    r = say(traced, sender, "123456")
    assert r.replies[0].template_id == "foreclosure_quote"

    rows = _spans(traced)
    roots = [x for x in rows if x.seq == 0]
    assert len(roots) == 3 and all(x.name == "turn" and x.status == "succeeded" for x in roots)
    last = roots[-1]
    assert last.output["path"][0] == "screen" and last.output["path"][-1] == "output_guard"
    nodes = [x for x in rows if x.trace_id == last.trace_id and x.seq]
    assert [n.name for n in nodes] == last.output["path"]
    # The OTP turn: the verified session runs the pending task and the gateway tool calls are attributed to it.
    ex = next(n for n in nodes if n.name == "execute")
    assert any(c["type"] == "tool_call" for c in ex.tool_calls)
    assert all(n.duration_ms >= 0 for n in nodes)


def test_spans_are_masked(traced, borrower):
    a, sender = borrower
    say(traced, sender, "emi due?")
    say(traced, sender, a["account_id"])
    say(traced, sender, "123456")
    blob = " ".join(str(x.input) + str(x.output) for x in _spans(traced))
    assert "123456" not in blob


def test_trace_id_is_opaque_and_stamped_on_audit_events(traced, borrower):
    from servicing_agent.platform.db import AuditEvent
    _, sender = borrower
    r = say(traced, sender, "emi due?")
    tid = _spans(traced)[-1].trace_id
    assert tid.startswith("tr_") and sender not in tid
    with traced.session_factory() as s:
        n = s.execute(select(func.count()).select_from(AuditEvent).where(AuditEvent.trace_id == tid)).scalar()
    assert n >= 2


def test_tracing_does_not_change_replies(settings, borrower, tmp_path):
    a, sender = borrower
    plain = build_runtime(replace(settings, database_url=f"sqlite:///{tmp_path}/plain.db"))
    traced_rt = build_runtime(replace(settings, tracing=True, database_url=f"sqlite:///{tmp_path}/traced.db"))

    def script(rt):
        out = []
        for t in ["what is my foreclosure amount", a["account_id"], "123456", "send my statement"]:
            r = say(rt, sender, t)
            out.append(([x.template_id for x in r.replies], r.resolution, r.verified, r.intents))
        return out

    assert script(plain) == script(traced_rt)


def test_node_failure_is_recorded_and_reraised(traced, borrower):
    _, sender = borrower
    def boom(*a, **k):
        raise RuntimeError("planner down")
    traced.planner.plan = boom
    # rebuild graph so nothing else changes; the planner is called at run time
    with pytest.raises(RuntimeError):
        say(traced, sender, "emi due?")
    rows = _spans(traced)
    failed = [x for x in rows if x.status == "failed"]
    assert {x.name for x in failed} == {"plan", "turn"}
    assert "planner down" in failed[0].error


def test_tracer_failure_never_breaks_a_turn(traced, borrower):
    _, sender = borrower
    def bad_sink(spans):
        raise OSError("db gone")
    traced.trace_sink = bad_sink
    assert say(traced, sender, "emi due?").replies


def test_traces_api(traced, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(traced.settings)
    rt = app.state.runtime
    say(rt, sender, "emi due?")
    say(rt, sender, a["account_id"])
    say(rt, sender, "123456")
    client = TestClient(app)
    lst = client.get("/admin/api/traces").json()
    assert len(lst) == 3 and lst[0]["path"]
    one = client.get(f"/admin/api/traces/{lst[0]['trace_id']}").json()
    assert one["turn"]["name"] == "turn" and one["spans"] and one["audit_events"]
    assert client.get("/admin/api/traces/tr_missing").status_code == 404
    assert client.get("/admin/api/traces/case/CASE_missing").status_code == 404


def test_case_timeline_tracks_each_tier(runtime):
    from servicing_agent.platform.api.traces import build_case_timeline
    from servicing_agent.platform.db import Case, CaseVersion, Decision
    from datetime import datetime
    cases = runtime.cases
    v1 = cases.create("CN0000000002", "conv", CaseRequest(type=CaseType.WAIVER, amount=2000, components=["penal"]))

    def timeline():
        with runtime.session_factory() as s:
            case = s.get(Case, v1.case_id)
            vs = s.execute(select(CaseVersion).where(CaseVersion.case_id == v1.case_id)
                           .order_by(CaseVersion.version)).scalars().all()
            ds = s.execute(select(Decision).where(Decision.case_id == v1.case_id)).scalars().all()
        return build_case_timeline(case, vs, ds, False, datetime.utcnow())["versions"][-1]["stages"]

    st = {x["role"]: x["state"] for x in timeline()}
    assert st == {"approver": "waiting", "credit_head": "skipped", "compliance": "not_reached"}
    a = cases.decide(v1.case_id, 1, DecisionIn(role="approver", decision="approve", rationale="ok",
                                               reviewer_id="r1", etag=v1.etag))
    st = {x["role"]: x["state"] for x in timeline()}
    assert st["approver"] == "approved" and st["compliance"] in ("waiting", "not_reached")
    # superseding supersedes the old version's stages
    v2 = cases.supersede(v1.case_id, 1, CaseRequest(type=CaseType.WAIVER, amount=1000, components=["penal"]), "changed")
    with runtime.session_factory() as s:
        case = s.get(Case, v1.case_id)
        vs = s.execute(select(CaseVersion).where(CaseVersion.case_id == v1.case_id)
                       .order_by(CaseVersion.version)).scalars().all()
        ds = s.execute(select(Decision).where(Decision.case_id == v1.case_id)).scalars().all()
    tl = build_case_timeline(case, vs, ds, False, datetime.utcnow())["versions"]
    assert tl[0]["status"] == "SUPERSEDED" and tl[1]["current"] and tl[1]["stages"][0]["state"] == "waiting"


# ---------------------------------------------------------------- Monitor + sequence

def _waiver_flow(rt, a, sender):
    for t in ["emi due?", a["account_id"], "123456", "waive my late charges, my salary was delayed", "yes"]:
        r = say(rt, sender, t)
    return r


def test_sequence_shows_turn_then_projected_approval_chain(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(replace(settings, tracing=True))
    rt = app.state.runtime
    _waiver_flow(rt, a, sender)
    client = TestClient(app)
    tid = client.get("/admin/api/traces").json()[0]["trace_id"]
    q = client.get(f"/admin/api/traces/{tid}/sequence").json()
    ev = q["events"]
    assert [e["n"] for e in ev] == list(range(1, len(ev) + 1))
    assert ev[0]["from"] == "Borrower" and ev[0]["to"] == "Ingress"
    assert {e["from"] for e in ev} | {e["to"] for e in ev} <= set(q["lanes"])
    # the turn created a case: gateway -> case service, and the approval chain is still ahead
    assert any(e["to"] == "Case service" and "create case" in e["label"] for e in ev)
    waiting = [e for e in ev if e["state"] == "waiting"]
    assert waiting and waiting[0]["to"] == "Approver"
    projected = [e for e in ev if e["state"] == "projected"]
    assert any(e["to"] == "Compliance" for e in projected) and projected[-1]["to"] == "Borrower"
    assert q["taken"][0] == "screen"

    # approve the first tier: that step becomes done, compliance becomes the waiting one
    case_id = client.get(f"/admin/api/traces/{tid}").json()["cases"][0]
    c = client.get("/admin/api/cases").json()[0]
    client.post(f"/admin/api/cases/{case_id}/versions/1/decisions",
                json={"role": "approver", "decision": "approve", "rationale": "ok", "reviewer_id": "r1", "etag": c["etag"]})
    ev = client.get(f"/admin/api/traces/{tid}/sequence").json()["events"]
    assert any(e["from"] == "Approver" and e["state"] == "done" and "approved" in e["label"] for e in ev)
    assert [e for e in ev if e["state"] == "waiting"][0]["to"] == "Compliance"


def test_monitor_aggregates_nodes_tools_and_cases(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(replace(settings, tracing=True))
    _waiver_flow(app.state.runtime, a, sender)
    m = TestClient(app).get("/admin/api/traces/monitor").json()
    assert m["turns"]["total"] == 5 and m["turns"]["failed"] == 0
    assert m["nodes"]["screen"]["runs"] == 5 and m["nodes"]["output_guard"]["runs"] == 5
    assert m["nodes"]["handoff"]["runs"] == 0                       # untouched nodes still listed (ghosted in the UI)
    assert sum(m["tools"].values()) > 0 and m["cases"].get("PENDING_APPROVER") == 1
    assert m["last_turn_at"]


def test_monitor_empty_and_unknown_trace(settings):
    from servicing_agent.platform.api.app import create_app
    client = TestClient(create_app(replace(settings, tracing=True)))
    m = client.get("/admin/api/traces/monitor").json()
    assert m["turns"]["total"] == 0 and m["last_turn_at"] is None
    assert client.get("/admin/api/traces/tr_nope/sequence").status_code == 404


# ---------------------------------------------------------------- Sessions list

def _root(conv, tid, age, status="succeeded", verified=True, text="hi"):
    from datetime import datetime, timedelta
    now = datetime(2026, 11, 20, 12, 0, 0)
    return TraceSpan(trace_id=tid, conversation_id=conv, seq=0, name="turn", status=status,
                     started_at=now - timedelta(seconds=age), duration_ms=5, input={"text": text},
                     output={"resolution": "auto", "verified": verified, "intents": ["emi_due_query"]})


def test_build_sessions_status_rules_and_masking():
    from datetime import datetime
    from servicing_agent.platform.api.traces import build_sessions
    now = datetime(2026, 11, 20, 12, 0, 0)
    roots = [_root("wa:919876543210", "t1", 5),                       # live
             _root("wa:911111111111", "t2", 120, status="failed"),    # failed beats everything
             _root("wa:912222222222", "t3", 600),                     # waiting on a reviewer
             _root("wa:913333333333", "t4", 900),                     # active
             _root("wa:914444444444", "t5", 99999),                   # idle
             _root("wa:919876543210", "t0", 400)]                     # older turn of the live one
    cases = {"wa:912222222222": [("C1", "PENDING_COMPLIANCE")], "wa:913333333333": [("C2", "APPROVED")]}
    got = {s["conversation_id"]: s for s in build_sessions(roots, cases, now)}
    assert set(got) == {"wa:919876543210", "wa:911111111111", "wa:912222222222", "wa:913333333333"}
    assert got["wa:919876543210"]["status"] == "live" and got["wa:919876543210"]["turns"] == 2
    assert got["wa:911111111111"]["status"] == "failed"
    assert got["wa:912222222222"]["status"] == "waiting_approval" and got["wa:912222222222"]["open_cases"][0]["case_id"] == "C1"
    assert got["wa:913333333333"]["status"] == "active" and got["wa:913333333333"]["open_cases"] == []
    assert got["wa:919876543210"]["label"] == "wa:••••3210"          # mobile never shown in full
    assert got["wa:919876543210"]["last_trace_id"] == "t1"
    all_ = build_sessions(roots, cases, now, include_idle=True)
    assert len(all_) == 5 and next(s for s in all_ if s["conversation_id"] == "wa:914444444444")["status"] == "idle"


def test_sessions_endpoint_lists_the_conversation(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(replace(settings, tracing=True))
    _waiver_flow(app.state.runtime, a, sender)
    rows = TestClient(app).get("/admin/api/traces/sessions").json()
    assert len(rows) == 1
    s = rows[0]
    assert s["status"] in ("live", "waiting_approval") and s["turns"] == 5 and s["verified"]
    assert s["open_cases"] and sender not in s["label"]
    assert TestClient(app).get(f"/admin/api/traces?conversation_id={s['conversation_id']}").json()


# ---------------------------------------------------------------- Categories + actionables

def _sp(name, status="succeeded", out=None, calls=None, error=None):
    return {"name": name, "status": status, "started_at": "2026-11-20T10:00:00", "duration_ms": 3,
            "input": {}, "output": out or {}, "tool_calls": calls or [], "error": error}


def _tl(role_state, waiting=0):
    stages = [{"role": "approver", "state": role_state, "waiting_seconds": waiting, "since": "2026-11-20T10:00:00",
               "reviewer": "r1", "rationale": "ok", "decided_at": "2026-11-20T10:01:00", "waited_seconds": 60},
              {"role": "credit_head", "state": "skipped"},
              {"role": "compliance", "state": "not_reached"}]
    return {"case_id": "C1", "versions": [{"version": 1, "status": "PENDING_APPROVER", "created_at": "2026-11-20T10:00:00",
                                           "stages": stages}]}


def test_every_step_has_a_category_and_an_action():
    from servicing_agent.platform.api.traces import CATEGORIES, build_sequence
    turn = {"input": {"text": "hi"}, "started_at": "t", "conversation_id": "wa:1", "trace_id": "tr_x"}
    spans = [_sp("screen"), _sp("verify", out={"resolutions": ["pending_verification"]}),
             _sp("execute", calls=[{"type": "tool_call", "tool": "lms.dues"}, {"type": "case_created", "case_id": "C1", "version": 1},
                                   {"type": "ticket_created", "queue": "waiver_approvals"}]),
             _sp("output_guard", out={"final_replies": [{"template_id": "x", "text": "t"}], "events": ["output_blocked"]})]
    ev = build_sequence(turn, spans, [_tl("waiting", 30)])
    assert ev and all(e["category"] in CATEGORIES and e["action"]["severity"] and "ref" not in e for e in ev)
    cats = {e["label"]: e["category"] for e in ev}
    assert cats["inbound message"] == "channel" and cats["screen"] == "understand" and cats["verify"] == "verify"
    assert cats["output_guard"] == "guard" and cats["reply: x"] == "deliver" and cats["send reply"] == "deliver"
    guard = next(e for e in ev if e["label"] == "output_guard")
    assert guard["action"]["severity"] == "attention" and guard["action"]["kind"] == "inspect"
    assert guard["action"]["target"] == "tr_x"
    ticket = next(e for e in ev if e["label"].startswith("ticket") and e["to"] == "Policy gateway")
    assert ticket["category"] == "escalate" and ticket["action"]["kind"] == "open_queue"
    wait = next(e for e in ev if e["state"] == "waiting")
    assert wait["category"] == "approval" and wait["action"]["severity"] == "waiting"
    assert wait["action"]["kind"] == "open_case" and wait["action"]["target"] == "C1"
    assert all(e["action"]["severity"] == "upcoming" for e in ev if e["state"] == "projected")


def test_overdue_review_and_failures_need_attention():
    from servicing_agent.platform.api.traces import APPROVAL_SLA_SECONDS, build_sequence
    turn = {"input": {}, "started_at": "t", "conversation_id": "wa:1", "trace_id": "tr_x"}
    ev = build_sequence(turn, [], [_tl("waiting", APPROVAL_SLA_SECONDS + 60)])
    w = next(e for e in ev if e["state"] == "waiting")
    assert w["action"]["severity"] == "attention" and "Overdue" in w["action"]["text"]

    ev = build_sequence(turn, [_sp("plan", status="failed", error="RuntimeError: boom")], [])
    f = next(e for e in ev if e["label"] == "plan")
    assert f["action"]["severity"] == "attention" and f["action"]["kind"] == "inspect"
    assert "boom" not in f["action"]["text"] and "boom" not in f["plain"]     # no error text for operators
    assert "boom" in f["tech"]["error"]                                       # but engineering can still get it

    ev = build_sequence(turn, [_sp("execute", calls=[{"type": "tool_denied", "tool": "lms.dues", "reason": "not_verified"}])], [])
    d = next(e for e in ev if e["action"]["severity"] == "blocked")
    assert d["category"] == "verify" and d["action"]["kind"] == "open_conversation" and d["action"]["target"] == "wa:1"

    rejected = _tl("rejected")
    ev = build_sequence(turn, [], [rejected])
    r = next(e for e in ev if e["label"] == "approver rejected")
    assert r["action"]["severity"] == "attention" and r["action"]["target"] == "C1"


def test_sequence_endpoint_reports_categories_and_needs_action(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(replace(settings, tracing=True))
    _waiver_flow(app.state.runtime, a, sender)
    client = TestClient(app)
    tid = client.get("/admin/api/traces").json()[0]["trace_id"]
    q = client.get(f"/admin/api/traces/{tid}/sequence").json()
    assert q["categories"]["approval"] == "Approval" and sum(q["by_category"].values()) == len(q["events"])
    assert q["conversation_id"] == client.get("/admin/api/traces").json()[0]["conversation_id"]
    need = q["needs_action"]
    assert any(e["action"]["kind"] == "open_case" and e["action"]["severity"] == "waiting" for e in need)
    assert len([e for e in need if e["action"]["kind"] == "open_queue"]) == 1   # the approval ticket, raised once
    assert next(e for e in q["events"] if e["label"] == "run turn")["category"] == "channel"


# ---------------------------------------------------------------- Operator wording + lifecycle

JARGON = ("gateway", "trace", "span", "tool_call", "tool_denied", "lms.", "payments.", "claims.", "_", "json", "etag",
          "supersed", "maker-checker", "resolutions", "pending_verification")


def test_operator_text_has_no_internal_names(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(replace(settings, tracing=True))
    _waiver_flow(app.state.runtime, a, sender)
    client = TestClient(app)
    cid = client.get("/admin/api/traces/sessions").json()[0]["conversation_id"]
    data = client.get("/admin/api/traces/lifecycle", params={"conversation_id": cid}).json()
    steps = [e for t in data["turns"] for e in t["events"]]
    assert steps
    for e in steps:
        words = (e["plain"] + " " + e["action"]["text"] + " " + (e["stage_label"] or "")).lower()
        assert not [j for j in JARGON if j in words], (e["plain"], e["action"]["text"])
    assert all(label and "_" not in label for label in data["stages"].values())
    assert set(data["about"]) <= set(data["stages"])


def test_lifecycle_whole_journey_and_per_node(settings, borrower):
    from servicing_agent.platform.api.app import create_app
    a, sender = borrower
    app = create_app(replace(settings, tracing=True))
    _waiver_flow(app.state.runtime, a, sender)
    client = TestClient(app)
    cid = client.get("/admin/api/traces/sessions").json()[0]["conversation_id"]
    d = client.get("/admin/api/traces/lifecycle", params={"conversation_id": cid}).json()
    assert len(d["turns"]) == 5 and d["turns"][0]["text"] == "emi due?" and d["turns"][-1]["text"] == "yes"   # oldest first
    assert d["label"].startswith("wa:••••") and len(d["cases"]) == 1
    nodes = [e["node"] for t in d["turns"] for e in t["events"] if e["node"]]
    assert {"ingress", "screen", "verify", "plan", "execute", "output_guard", "case", "approver"} <= set(nodes)
    assert "tech" in d["turns"][0]["events"][2]                             # engineers can still reach the detail
    # the case's approval lifecycle is part of the whole picture
    assert d["cases"][0]["versions"][0]["stages"][0]["state"] == "waiting"
    assert client.get("/admin/api/traces/lifecycle", params={"conversation_id": "wa:none"}).status_code == 404


def test_voice_note_trace_shows_what_was_said():
    from servicing_agent.contracts.state import InboundMessage, MediaEvidence
    from servicing_agent.platform.tracing import summarize_input
    msg = InboundMessage(message_id="v1", sender="919999999999", type="voice",
                         media=MediaEvidence(kind="voice_note", transcript="what are the details", provider="f"))
    out = summarize_input({"inbound": msg, "text": None, "session": {}}, mask=lambda t: t)
    assert out["text"] == "what are the details" and out["type"] == "voice"
