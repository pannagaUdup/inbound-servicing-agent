# Inbound WhatsApp Servicing Agent

Agentathon build for the **inbound WhatsApp servicing agent**: resolve routine borrower
requests ("already paid", receipts, statements, foreclosure amounts, dues) within minutes,
and route everything else (disputes, complaints, waivers, settlements) to the right queue
with full context.

The design is in [docs/DESIGN.md](docs/DESIGN.md). The three-team split is in
[docs/TEAMS.md](docs/TEAMS.md). Open decisions and their defaults are in
[docs/DECISIONS.md](docs/DECISIONS.md).

## Design in one paragraph

This is one bounded LangGraph state machine, not a multi-agent system. LLMs do
perception (speech-to-text, screenshot extraction), planning across intents and the
wording of replies. Deterministic code owns every decision with consequences:

- **The D7 ledger decides payments.** A claim is accepted only on an exact D7 match, so
  every edited or fake screenshot ends in "not accepted + flagged".
- **The policy gateway enforces verification and account scope.** The model never holds
  unverified data, and it can never ask for another account.
- **Templates, the routing matrix and the case service own waivers and routing.** The
  agent cannot make a commitment, and approvals can never land on a superseded request.

## Quickstart

```bash
make setup      # venv + install (Python 3.11+)
make data       # synthetic D1/D2/D7 for 10,000 accounts -> data/generated/
make test
make run        # http://localhost:8000/docs
make eval       # scenario conversations through the agent; fails on any zero-tolerance metric
```

Try a turn:

```bash
curl -s localhost:8000/v1/simulate/messages -H 'content-type: application/json' \
  -d '{"sender":"919876543210","text":"what is my foreclosure amount"}' | jq
```

The OTP stub accepts `SA_OTP_STUB_CODE` (default `123456`). To use Claude for planning,
install the `llm` extra, set `SA_PLANNER=llm` and set `ANTHROPIC_API_KEY`. The rules
planner needs no key and is the fallback whenever the LLM call fails.

## Layout and ownership

| Path | Team | What |
|---|---|---|
| `src/servicing_agent/contracts/` | **All (frozen)** | Conversation state, tool signatures, case/decision API, event model |
| `src/servicing_agent/agent/` | **T1** Agent Core & Guardrails | LangGraph turn, verification, policy gateway, planner, handlers, screening, output guard, templates |
| `src/servicing_agent/platform/` | **T2** Platform, Integrations & Human Approval | FastAPI, mock tools, versioned case service, context pack, persistence, conversation store |
| `src/servicing_agent/data/` | **T3** Data, Perception & Evaluation | Synthetic data generators, data store, D7 reconciliation, perception stubs |
| `evals/` | **T3** | Scenario generator, deterministic evaluators, eval runner |
| `config/` | T2/T3 | Routing matrix, per-intent field allow-lists, policy bands |
| `tests/` | All | Unit and end-to-end tests |

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/simulate/messages` | One synchronous turn (eval harness and demo) |
| GET/POST | `/webhooks/whatsapp` | BSP handshake and inbound messages |
| GET | `/v1/conversations/{id}` | Transcript, events and tickets |
| GET | `/v1/tickets?queue=` | Routed tickets |
| GET | `/v1/cases/{case_id}` | Case with every version and decision |
| GET | `/v1/cases/{case_id}/versions/{v}/context` | Context pack for approver and compliance |
| POST | `/v1/cases/{case_id}/versions/{v}/decisions` | Approver/compliance decision (409 if superseded or stale) |
| GET | `/healthz` | Health |

## Parked (by decision)

- Screenshot tamper forensics (ELA, EXIF, layout). The D7 match still catches every fake.
- Dunning suppression on verified payment.
