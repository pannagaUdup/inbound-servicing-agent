"""Builds the full runtime (tools, cases, planner, store, channel, campaigns) from settings."""

from __future__ import annotations

from servicing_agent.agent.ack import make_ack_classifier
from servicing_agent.agent.graph import AgentRuntime
from servicing_agent.agent.planner import make_planner
from servicing_agent.config import Settings
from servicing_agent.data.store import load_store
from servicing_agent.platform.campaigns import CampaignService
from servicing_agent.platform.cases import CaseService
from servicing_agent.platform.channel import MockChannel, WhatsAppChannel
from servicing_agent.platform.conversations import ConversationStore
from servicing_agent.platform.email import make_email_sender
from servicing_agent.platform.inbound import InboundFlow
from servicing_agent.platform.otp import RedisOtpService
from servicing_agent.platform.db import AuditEvent, make_session_factory
from servicing_agent.platform.tools_mock import MockToolSet


def _make_channel(settings: Settings):
    if settings.whatsapp_token and settings.wa_phone_number_id:
        return WhatsAppChannel(settings)
    return MockChannel()  # dev / sim: records sends, no token needed


def build_runtime(settings: Settings) -> AgentRuntime:
    store = load_store(str(settings.data_dir))
    session_factory = make_session_factory(settings.database_url)
    tools = MockToolSet.from_store(store, settings.otp_stub_code)
    conversations = ConversationStore()
    channel = _make_channel(settings)

    def audit_sink(conversation_id: str, events: list[dict]):
        with session_factory.begin() as s:
            s.add_all(AuditEvent(conversation_id=conversation_id, type=e["type"], data=e.get("data", {}))
                      for e in events)

    runtime = AgentRuntime(
        settings=settings,
        tools=tools,
        cases=CaseService(session_factory, settings.restarts_flag_threshold_7d),
        planner=make_planner(settings.planner, settings.planner_model),
        conversations=conversations,
        audit_sink=audit_sink,
    )
    runtime.store = store
    runtime.session_factory = session_factory
    runtime.channel = channel
    runtime.campaigns = CampaignService(
        session_factory, store, channel=channel, tools=tools, settings=settings,
        conversations=conversations, ack=make_ack_classifier("mock"),
    )
    otp_service = RedisOtpService(settings.redis_url, ttl_seconds=settings.otp_ttl_seconds,
                                  max_attempts=settings.max_otp_attempts)
    runtime.inbound = InboundFlow(store, channel, make_email_sender(settings), conversations, settings,
                                  otp=otp_service)
    return runtime
