"""Builds the full runtime (tools, cases, planner, store) from settings."""

from __future__ import annotations

from servicing_agent.agent.graph import AgentRuntime
from servicing_agent.agent.planner import make_planner
from servicing_agent.config import Settings
from servicing_agent.data.store import load_store
from servicing_agent.platform.cases import CaseService
from servicing_agent.platform.conversations import ConversationStore
from servicing_agent.platform.db import AuditEvent, make_session_factory
from servicing_agent.platform.tools_mock import MockToolSet


def build_runtime(settings: Settings) -> AgentRuntime:
    store = load_store(str(settings.data_dir))
    session_factory = make_session_factory(settings.database_url)

    def audit_sink(conversation_id: str, events: list[dict]):
        with session_factory.begin() as s:
            s.add_all(AuditEvent(conversation_id=conversation_id, type=e["type"], data=e.get("data", {}))
                      for e in events)

    runtime = AgentRuntime(
        settings=settings,
        tools=MockToolSet.from_store(store, settings.otp_stub_code),
        cases=CaseService(session_factory, settings.restarts_flag_threshold_7d),
        planner=make_planner(settings.planner, settings.planner_model),
        conversations=ConversationStore(),
        audit_sink=audit_sink,
    )
    runtime.store = store
    return runtime
