# Team split and actionables

Three teams, each owning one folder. `src/servicing_agent/contracts/` is shared and frozen.

## Interface freeze (first 4 hours)

Before building on them, all three teams agree on these interfaces and then freeze them:

| Interface | File | Owner |
|---|---|---|
| Data schemas (D1, D2, D7, waiver history) | `data/generators/core.py`, `data/store.py` | T3 |
| Tool signatures | `contracts/tools.py` | T2 |
| Case / decision API | `contracts/cases.py`, `platform/cases.py` | T2 |
| Conversation state, intents, event model | `contracts/state.py`, `contracts/events.py` | T1 |
| Routing matrix, per-intent field lists, policy bands | `config/*.yaml` | T2/T3 |

Any change after the freeze needs a PR approved by one person from each team.

---

## T1: Agent Core & Guardrails ("what the agent decides and says")

| Pri | Task | Where | Done when |
|---|---|---|---|
| P0 | LangGraph turn: screen → verify/plan → execute → output guard | `agent/graph.py` | All tests in `tests/test_agent_flow.py` pass ✅ scaffolded |
| P0 | Verification: account number + sender in that account's active contacts + OTP; one account per session; neutral replies | `agent/verification.py` | Wrong account, unregistered number and shared-number cases never disclose ✅ scaffolded |
| P0 | Policy gateway: injects `scope.account_id`, per-intent field allow-lists, records allowed values | `agent/gateway.py` | Cross-account disclosure = 0 on eval ✅ scaffolded |
| P0 | Output guard: disclosure, numeric grounding, commitment language | `agent/output_guard.py` | Pre-verification disclosure = 0; commitment = 0 ✅ scaffolded |
| P0 | Multi-intent planner (rules now, Claude structured output behind `SA_PLANNER=llm`) | `agent/planner.py` | Intent accuracy ≥ 90% on dev set |
| P0 | Changed waiver detection + borrower confirmation before superseding | `agent/handlers.py` | Material-change accuracy ≥ 95% ✅ scaffolded |
| P1 | Injection classifier (prompt-guard class model) behind `screen()` | `agent/screening.py` | Injection subset: 100% no policy deviation |
| P1 | Templates for kn, ta, te, mr, bn; language detection | `agent/templates.py` | Reply language match ≥ 98% |
| P1 | Recent contact change (< 7 days) → human check before documents/waivers | `agent/verification.py`, handlers | Flag shown in context pack |
| P2 | LLM tone smoothing over templates (output guard still applies) | new `agent/compose.py` | No regression on zero-tolerance metrics |

## T2: Platform, Integrations & Human Approval ("what the agent runs on")

| Pri | Task | Where | Done when |
|---|---|---|---|
| P0 | `/v1/simulate/messages` + conversation and ticket endpoints | `platform/api/app.py` | Eval harness runs end to end ✅ scaffolded |
| P0 | Mock tools over synthetic data (LMS, D7, OTP, docs, pay links, ticketing) | `platform/tools_mock.py` | Same signatures as `contracts/tools.py` ✅ scaffolded |
| P0 | Versioned case service: supersede in one transaction; version + etag guard → 409 | `platform/cases.py` | Concurrency test (decide vs supersede ×1000) → 0 approvals on cancelled versions ✅ scaffolded (×200 in tests) |
| P0 | Context pack per version, every number from source records | `platform/context_pack.py` | 100% required sections present ✅ scaffolded |
| P0 | Webhook → queue → workers with per-conversation lock; 3 s message batching | `platform/api/app.py`, new `platform/worker.py` | 500 concurrent conversations, p95 first response < 20 s |
| P1 | Approval workflow in LangGraph: `build_context → policy_band → interrupt(approver) → interrupt(compliance) → communicate(template)`, Postgres checkpointer, keyed `case:{id}:v{n}` | new `platform/approvals.py` | Superseded version can't be resumed |
| P1 | Reviewer console (approver/compliance) showing the context pack, version diffs, live cancel of v1 | new `console/` | Demo: changed amount cancels v1 live |
| P1 | WhatsApp simulator UI for the demo | new `console/` | Demo script runs without a real BSP |
| P1 | Postgres + Redis (docker-compose), conversation store out of memory | `platform/conversations.py`, `platform/db.py` | Restart keeps sessions |
| P2 | Dashboard (Prometheus/Grafana), load test | new `ops/` | Alerts from DESIGN.md §11 wired |

## T3: Data, Perception & Evaluation ("what the agent learns from and is measured by")

| Pri | Task | Where | Done when |
|---|---|---|---|
| P0 | Synthetic D1/D2/D7, waiver history, seeded and validated | `data/generators/core.py` | `make data` + `validate()` clean ✅ scaffolded |
| P0 | D7 claim verifier (exact match, OCR tolerance, settlement window, replay, other-account). Code moved to T1's `agent/tools/`, since only the graph calls it; T3 owns its test cases | `agent/tools/reconciliation.py` | 100% of seeded fakes not accepted + flagged ✅ scaffolded |
| P0 | Scenario conversation generator, labels from spec (not an LLM judge), 300 dev + 300 held-out | `evals/scenarios.py` | Covers every case in DESIGN.md §4 ✅ scaffolded (text-only) |
| P0 | Eval harness with zero-tolerance gates; non-zero exit blocks merge | `evals/run_eval.py` | Runs in CI ✅ scaffolded |
| P1 | LLM-written borrower turns from persona + spec (held-out set: different seed, personas and generator model) | `evals/scenarios.py` | Never used for prompt tuning |
| P1 | Speech-to-text (Sarvam / IndicConformer, Whisper fallback) + TTS-generated voice notes with noise | `data/perception.py` | WER tracked per language |
| P1 | Receipt extraction (VLM + PaddleOCR cross-check); 500 generic UPI-receipt images rendered with Playwright, genuine + edited | `data/perception.py`, new `data/generators/screenshots.py` | Extraction field accuracy tracked |
| P1 | Red-team suites: auth bypass, cross-account probing, injection via text/image/voice | `evals/` | 0 bypasses |
| P2 | LangSmith datasets + experiments per prompt/model version | `evals/` | Regression dataset grows with every incident |
| Parked | Screenshot tamper forensics, dunning suppression | — | — |

## Milestones

1. **H+4**: interfaces frozen; `make data`, `make test` green on main.
2. **H+12**: end-to-end text flows (verification, dues, receipt, payment claim, routing) via `/simulate`.
3. **H+20**: waiver/settlement versioning with confirmation; context pack; reviewer console v0.
4. **H+28**: voice + image perception; eval on 300 dev conversations; zero-tolerance gates green.
5. **H+36**: demo script rehearsed; held-out eval run once.

## Working rules

- `main` is always green: `make test` and `make eval` must pass before merge.
- Account data only goes through `PolicyGateway`. Never call `runtime.tools` from agent code.
- No number reaches the borrower unless it came from a tool result this turn.
- No commitment wording on waivers, settlements or charges, in any language.
