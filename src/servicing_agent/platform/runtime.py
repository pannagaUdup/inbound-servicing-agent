"""Builds the full runtime (tools, cases, planner, store, channel, campaigns) from settings."""

from __future__ import annotations

import logging

import os

from servicing_agent.agent.tools.reconciliation import india_now

from servicing_agent.agent.ack import make_ack_classifier
from servicing_agent.agent.graph import AgentRuntime
from servicing_agent.agent.answerer import SarvamAnswerer
from servicing_agent.agent.planner import FallbackPlanner, SarvamIntentClassifier, make_planner
from servicing_agent.config import Settings, policy_bands
from servicing_agent.data.store import load_store
from servicing_agent.platform.campaigns import CampaignService
from servicing_agent.platform.cases import CaseService
from servicing_agent.agent.normalizer import SarvamNormalizer
from servicing_agent.data.perception.sarvam import SarvamClient, SarvamConfig
from servicing_agent.platform.claims import ClaimLedger
from servicing_agent.platform.evidence import EvidenceRecorder
from servicing_agent.platform.channel import MockChannel, WhatsAppChannel
from servicing_agent.platform.conversations import ConversationStore
from servicing_agent.platform.email import make_email_sender
from servicing_agent.platform.inbound import InboundFlow
from servicing_agent.platform.otp import RedisOtpService
from servicing_agent.platform.otp_adapter import AgentOtpAdapter
from servicing_agent.platform.db import AuditEvent, make_session_factory
from servicing_agent.platform.agencies import AgencyService, seed_agencies
from servicing_agent.platform.docgen import PdfDocGen
from servicing_agent.platform.metrics import MetricsService
from servicing_agent.platform.ticketing import TicketService
from servicing_agent.platform.paylinks import PayLinkService
from servicing_agent.platform.tools_mock import MockToolSet


def _make_channel(settings: Settings):
    if settings.whatsapp_token and settings.wa_phone_number_id:
        return WhatsAppChannel(settings)
    return MockChannel()  # dev / sim: records sends, no token needed


def build_runtime(settings: Settings) -> AgentRuntime:
    if settings.data_source == "db":
        from servicing_agent.data.db_store import DbDataStore
        store = DbDataStore(settings.database_url, settings.data_refresh_seconds)
        logging.getLogger(__name__).info("loan data: Postgres (refresh every %ss)", settings.data_refresh_seconds)
    else:
        store = load_store(str(settings.data_dir))
    session_factory = make_session_factory(settings.database_url)
    tools = MockToolSet.from_store(store, settings.otp_stub_code)
    # Real PDF document generation (receipts, statements, foreclosure quotes, NOCs).
    tools.docs = PdfDocGen(store, settings.docs_dir)
    # Real DB-backed ticketing: routed requests persist to the tickets table, assigned to one ops user.
    tools.tickets = TicketService(session_factory, assignee=settings.ops_assignee)
    # Payment-claim ledger in the database: audit trail + replay guard that survives restarts.
    claims = ClaimLedger(session_factory)
    tools.payments.ledger = claims
    # Share session state via Redis for the real (Postgres/multi-process) setup; for a sqlite dev/test
    # run, stay in-process + sqlite so tests don't contaminate the shared dev Redis across runs.
    _conv_redis = None if settings.database_url.startswith("sqlite") else settings.redis_url
    conversations = ConversationStore(redis_url=_conv_redis, session_factory=session_factory)
    channel = _make_channel(settings)

    def audit_sink(conversation_id: str, events: list[dict]):
        with session_factory.begin() as s:
            now = india_now()                       # audit rows are in India time (IST wall clock)
            s.add_all(AuditEvent(conversation_id=conversation_id, type=e["type"], data=e.get("data", {}), at=now,
                                 trace_id=e.get("trace_id"))
                      for e in events)

    # Real DB-backed pay-links + mocked payment (clickable checkout page -> UTR + D7 txn + overdue
    # deduction + receipt). Paid links are replayed onto the store so deltas survive a restart.
    tools.paylinks = PayLinkService(session_factory, store, docs=tools.docs, channel=channel,
                                    audit=audit_sink, conversations=conversations,
                                    public_base_url=settings.public_base_url)
    tools.paylinks.replay_paid()

    runtime = AgentRuntime(
        settings=settings,
        tools=tools,
        cases=CaseService(session_factory, settings.restarts_flag_threshold_7d, assignee=settings.ops_assignee,
                          store=store, bands_cfg=policy_bands()),
        planner=make_planner(settings.planner, settings.planner_model),
        conversations=conversations,
        audit_sink=audit_sink,
        claims=claims,
    )
    runtime.claims = claims
    if settings.tracing:
        from servicing_agent.platform.tracing import make_trace_sink
        runtime.trace_sink = make_trace_sink(session_factory)
    # Policy document retrieval: hybrid BM25 + embeddings when available, keyword-only otherwise.
    from servicing_agent.agent import policy as policy_mod
    policy_mod.configure(policy_mod.make_embedder(settings.embeddings, settings.embedding_model or None,
                                                  os.environ.get("SARVAM_API_KEY")))
    runtime.evidence = EvidenceRecorder(session_factory)   # full media / extraction / translation audit
    if settings.normalizer == "sarvam":
        sarvam = SarvamClient(SarvamConfig.from_env())
        models = [settings.normalizer_model, settings.normalizer_fallback_model]
        runtime.normalizer = SarvamNormalizer(sarvam, model=settings.normalizer_model,
                                              fallback_model=settings.normalizer_fallback_model or None)
        # Same Sarvam key: label messages the rules can't, and answer free-form loan questions.
        intent_models = models
        if settings.intent_model:
            fb = settings.intent_fallback_model or settings.normalizer_model
            intent_models = [settings.intent_model] + ([] if fb.lower() == "none" or fb == settings.intent_model else [fb])
        runtime.planner = FallbackPlanner(runtime.planner, SarvamIntentClassifier(sarvam, intent_models))
        logging.getLogger(__name__).info("intent classifier models: %s", intent_models)
        runtime.answerer = SarvamAnswerer(sarvam, models)
        # Re-phrase approved replies naturally (in the borrower's language); template is the fallback.
        from servicing_agent.agent.reply_writer import ReplyWriter
        runtime.reply_writer = ReplyWriter(sarvam, models)
    runtime.store = store
    runtime.paylinks = tools.paylinks   # the mock checkout route (/pay/{id}) reaches it here
    runtime.session_factory = session_factory
    runtime.channel = channel
    runtime.campaigns = CampaignService(
        session_factory, store, channel=channel, tools=tools, settings=settings,
        conversations=conversations, ack=make_ack_classifier("mock"),
    )
    otp_service = RedisOtpService(settings.redis_url, ttl_seconds=settings.otp_ttl_seconds,
                                  max_attempts=settings.max_otp_attempts)
    email_sender = make_email_sender(settings)
    from servicing_agent.platform.contact_email import ContactEmails
    contact_emails = ContactEmails(session_factory)
    runtime.contact_emails = contact_emails
    runtime.inbound = InboundFlow(store, channel, email_sender, conversations, settings, otp=otp_service,
                                  audit=audit_sink, emails=contact_emails, tickets=tools.tickets)
    # The agent's own verification step must use the same real OTP on a live channel; the stub
    # (which accepts SA_OTP_STUB_CODE) is only for dev, simulate and tests.
    live = isinstance(channel, WhatsAppChannel)
    if settings.otp_mode == "real" or (settings.otp_mode == "auto" and live):
        tools.otp = AgentOtpAdapter(otp_service, email_sender, store, settings, emails=contact_emails)
    runtime.otp_mode = "real" if isinstance(tools.otp, AgentOtpAdapter) else "stub"
    if live:
        # Payment evidence shared in a chat outlives session expiry and restarts (24 h, Redis).
        from servicing_agent.platform.session_context import SessionContextStore
        runtime.session_context = SessionContextStore.from_url(settings.redis_url)
    runtime.metrics = MetricsService(session_factory, store, tools.tickets)
    seed_agencies(session_factory)
    runtime.agencies = AgencyService(session_factory, store, tools.tickets, conversations=conversations)
    return runtime
