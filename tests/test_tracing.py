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
