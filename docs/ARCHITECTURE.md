# Architecture and flows

These are the mermaid sources for the eight diagrams on the architecture page
([architecture-flows.html](architecture-flows.html)); GitHub renders them inline. The
diagrams show the **target design** agreed on 19 Nov 2026. Some parts aren't built yet.
[Design vs. current code](#design-vs-current-code) at the end lists every difference.

1. [System architecture](#1-system-architecture)
2. [End-to-end message sequence](#2-end-to-end-message-sequence)
3. [Conversation graph (LangGraph)](#3-conversation-graph-langgraph)
4. [Verification: account number + mobile + OTP](#4-verification-account-number--mobile--otp)
5. [Payment-claim reconciliation](#5-payment-claim-reconciliation)
6. [Waiver / settlement case states](#6-waiver--settlement-case-states)
7. [Supersession and stale-approval guard](#7-supersession-and-stale-approval-guard)
8. [Evaluation and CI loop](#8-evaluation-and-ci-loop)

## 1. System architecture

The system has eight layers. Every call that touches account data goes from the
LangGraph agent through the deterministic policy gateway. The gateway injects the
verified `account_id`, so the model can't reach another account.

- Ingress only acknowledges and enqueues, and workers do the processing. That keeps the
  webhook under 100 ms.
- The per-conversation lock keeps a borrower's messages in order. Messages sent within
  3 s of each other are handled as one turn.
- Voice and image turns get a holding reply within 3 s.
- Waiver and settlement decisions arrive through the case API. They resume an approval
  thread only if that request version is still current.

```mermaid
flowchart TB
  subgraph CH["1 · Channels & users"]
    BOR["Borrower on WhatsApp<br/>text · voice · image"]
    SIM["Web simulator / eval harness"]
    REV["Approver & compliance<br/>reviewer console"]
    OPS["Ops & back office<br/>dashboard"]
  end
  subgraph ING["2 · Ingress — FastAPI"]
    WH["POST /webhooks/whatsapp<br/>HMAC verify · dedupe msg_id · 200 in under 100 ms"]
    SIMAPI["POST /v1/simulate"]
    CASEAPI["Case & decision API<br/>etag · 409 on stale version"]
  end
  subgraph Q["3 · Async backbone"]
    RS[("Redis Streams<br/>partitioned by conversation")]
    LOCK["Per-conversation lock<br/>3 s message batching"]
    ACK["Holding reply under 3 s<br/>for voice / image"]
  end
  subgraph W["4 · Worker pool — K8s HPA on queue lag"]
    PER["Perception<br/>ASR · image classify · VLM + OCR extract"]
    AG["LangGraph conversation graph<br/>screen → plan → verify → execute → compose"]
    OG["Output guard<br/>disclosure · cross-account · commitment · grounding"]
    SEND["Outbound sender<br/>idempotent"]
  end
  subgraph GW["5 · Policy gateway — deterministic"]
    SC["Scope injection<br/>account_id from session only"]
    AL["Per-intent field allowlist"]
    RL["Rate limits · lockouts"]
  end
  subgraph T["6 · Tools & systems of record"]
    LMS["LMS — D1"]
    D7["Payment feed — D7"]
    OTP["OTP service"]
    DOC["Receipt · statement · quote PDF"]
    PL["Payment link"]
    TK["Ticketing + routing matrix"]
    CS["Case service<br/>waiver / settlement versions"]
  end
  subgraph HITL["7 · Human approval"]
    AGR["Approval graph per version<br/>interrupt: approver → compliance"]
    CP["Context pack builder<br/>source-linked, numbers verified"]
  end
  subgraph DS["8 · Data & observability"]
    PG[("Postgres<br/>audit · conversations · cases · tickets · checkpoints")]
    RD[("Redis<br/>sessions · locks")]
    S3[("S3 / MinIO<br/>media, KMS")]
    LS["LangSmith<br/>traces · datasets · evals"]
    PM["Prometheus · Grafana · OTel"]
  end
  BOR -->|message| WH
  SIM -->|replay turn| SIMAPI
  WH -->|enqueue| RS
  SIMAPI -->|enqueue| RS
  RS --> LOCK
  LOCK --> ACK
  LOCK --> PER
  PER -->|normalized turn| AG
  AG -->|typed tool calls| SC
  SC --> AL --> RL
  RL --> LMS & D7 & OTP & DOC & PL & TK & CS
  AG -->|draft reply| OG
  OG --> SEND
  ACK --> SEND
  SEND -->|reply + PDF| BOR
  CS -->|start / cancel thread| AGR
  AGR --> CP
  REV -->|decide| CASEAPI
  CASEAPI -->|resume if version current| AGR
  AG -. state .-> RD
  AG -. checkpoints · audit .-> PG
  PER -. media .-> S3
  AG -. traces .-> LS
  PG -. event feed .-> OPS
  W -. metrics .-> PM
```

## 2. End-to-end message sequence

A verified borrower sends a Kannada voice note asking for a statement. The PDF arrives
about 8–12 s later. The time breaks down as roughly 0.1 s ingress, 1–2 s holding reply,
2–4 s speech-to-text, about 1 s planning, under 1 s tools, and 1–2 s compose and guard.
Every account call passes the gateway, and every step writes an audit event.

```mermaid
sequenceDiagram
  autonumber
  actor B as Borrower
  participant BSP as WhatsApp Cloud API
  participant API as FastAPI ingress
  participant Q as Redis Streams
  participant W as Worker
  participant P as Perception
  participant G as LangGraph
  participant GW as Policy gateway
  participant T as LMS · D7 · DocGen
  participant A as Audit · LangSmith
  B->>BSP: voice note "statement kalisi" (Kannada)
  BSP->>API: webhook
  API->>API: HMAC verify · dedupe
  API->>Q: enqueue (conversation partition)
  API-->>BSP: 200 under 100 ms
  Q->>W: deliver · take conversation lock
  W-->>B: holding reply under 3 s
  W->>P: ASR → text + lang=kn
  P->>G: normalized turn
  G->>G: screen → plan [statement_request]
  Note over G: session already verified (scope = 1 account)
  G->>GW: doc_generate(soa)
  GW->>GW: check verified · inject account_id · allowlist
  GW->>T: generate SOA PDF
  T-->>GW: pdf_url
  GW-->>G: allowed fields only
  G->>G: compose (kn template) → output_guard
  G->>W: reply + PDF
  W->>BSP: send
  BSP->>B: statement PDF (~8–12 s total)
  G->>A: audit events + trace
```

## 3. Conversation graph (LangGraph)

This is the bounded state machine every turn runs through. The LLM is used only in
normalize, plan and compose. Lane selection, handlers and the output guard are code.

- Distress, legal threats and harassment skip planning and go straight to a human.
- A detected injection is logged, and only the legitimate part of the message is
  handled.
- Low confidence gets one clarifying question. If the borrower is still unclear, the
  conversation is routed to a person.

```mermaid
flowchart TD
  IN(["Inbound turn — batched messages"]) --> N["normalize<br/>ASR · image extraction · language ID"]
  N --> S{"screen<br/>injection · distress · abuse"}
  S -->|distress · legal · harassment| H["P1 ticket + human handoff<br/>automation paused"]
  S -->|injection detected| LOGI["log injection_event<br/>keep legitimate part only"]
  S -->|clean| P["plan<br/>multi-intent task list + confidence"]
  LOGI --> P
  P -->|confidence under 0.6| CL{"already clarified once?"}
  CL -->|no| ASK["clarifying question<br/>quick-reply buttons"]
  CL -->|yes| RT
  P --> G{"task needs<br/>account data?"}
  G -->|yes, unverified| V["verification subflow<br/>account no. + mobile + OTP"]
  V -->|verified| EX
  V -->|failed / locked| DENY["neutral denial<br/>no account data"]
  G -->|no, or verified| EX["execute_tasks — loop per task"]
  EX --> R{"intent lane"}
  R -->|routine| BOT["code handler<br/>LMS · D7 · DocGen · PayLink"]
  R -->|payment claim| PC["D7 claim verifier"]
  R -->|waiver / settlement| WV["case flow<br/>create · supersede · status"]
  R -->|dispute · complaint · other| RT["ticket via routing matrix<br/>+ structured summary"]
  BOT --> C
  PC --> C
  WV --> C
  RT --> C
  ASK --> C
  DENY --> C
  H --> C
  C["compose<br/>localized template + tone"] --> OGD{"output_guard"}
  OGD -->|pass| OUT(["send reply"])
  OGD -->|violation| SAFE["safe template<br/>log guardrail event"]
  SAFE --> OUT
  OUT --> AU["audit events → Postgres + websocket + LangSmith"]
```

## 4. Verification: account number + mobile + OTP

The borrower gives a loan account number. The sender's WhatsApp number must be an
active registered contact of that account, and an OTP then confirms it. The reply is the
same whether or not the details match. A session covers one account; asking about
another loan means verifying that one, which replaces the scope.

```mermaid
flowchart TD
  A(["Task needs account data · session unverified"]) --> B["Ask for loan account number"]
  B --> C{"Format + check digit valid?"}
  C -->|no| B2["'That doesn't look valid'<br/>failure +1"]
  B2 --> LIM
  C -->|yes| D{"Account exists AND sender number<br/>is an ACTIVE contact linked to it?"}
  D -->|no| U["Uniform reply:<br/>'If details match, you'll get an OTP'<br/>no OTP sent · failure +1"]
  U --> LIM{"≥5 failures per sender in 24 h<br/>or ≥3 numbers failing on one account?"}
  LIM -->|yes| BLK["Block + security / fraud event"]
  LIM -->|no| B
  D -->|yes| SW{"Contact added under 7 days ago?"}
  SW -->|yes| FLAG["SIM-swap flag<br/>docs & waivers need human check"]
  FLAG --> O
  SW -->|no| O["otp_send(contact_id)<br/>to the matched registered number"]
  O --> E{"OTP correct?<br/>3 attempts · 5 min validity"}
  E -->|wrong, attempts left| O
  E -->|3 failures| LK["30-min lock + helpline"]
  E -->|yes| OK["Bind scope<br/>customer_id · account_id · mobile_hash<br/>idle 30 min · absolute 120 min"]
  OK --> NX(["Continue — this one account only"])
  NX -. asks about another loan .-> RE["Verify the other account<br/>→ replaces scope"]
```

## 5. Payment-claim reconciliation

A screenshot is something to search with, never proof of payment. Only an exact D7 match
marks a payment as paid. Edited and reused proofs go to the fraud queue, and the
borrower gets a neutral reply. Forensics and dunning suppression are parked (dashed
boxes).

```mermaid
flowchart TD
  A(["Payment claim — typed UTR or screenshot"]) --> V{"Session verified?"}
  V -->|no| HOLD["Store media · extract now<br/>reconcile only after verification"]
  HOLD --> VER["verification subflow"]
  VER --> X
  V -->|yes| X["Extract twice: VLM + PaddleOCR<br/>UTR · amount · time · payee · status"]
  X --> Q{"Fields readable?"}
  Q -->|no| ASKU["Ask for typed UTR or clearer image — once<br/>then route"]
  Q -->|yes| L["payment_feed_search — account-scoped<br/>UTR → fallback amount ±1 day + account<br/>OCR slip allowed: 1 candidate, edit distance ≤1"]
  L --> M{"D7 result"}
  M -->|"exact: success · amount · lender VPA ·<br/>this account · not already claimed"| PAID["PAID<br/>receipt via D7 txn_id"]
  M -->|UTR found, field mismatch| ED["EDITED<br/>Fraud queue P1 + field diff"]
  M -->|UTR claimed by another account| RU["REUSED<br/>Fraud queue P1"]
  M -->|not found| NF["NOT_FOUND<br/>Recon queue P3"]
  PAID --> REP["Confirmation + receipt PDF"]
  ED --> NEU["Neutral reply + reference + SLA<br/>never says 'fake'"]
  RU --> NEU
  NF --> NEU
  PAID -.-> DUN["Dunning suppression — PARKED"]:::parked
  NF -.-> FOR["Forensics: ELA · hash · tamper score — PARKED"]:::parked
  classDef parked stroke-dasharray: 5 5,fill:#f4f4f4,color:#888,stroke:#aaa
```

## 6. Waiver / settlement case states

Each request version goes from the approver to compliance. A confirmed material change
moves the current version to `SUPERSEDED` from any non-final state, and v+1 restarts from
`DRAFT`. A material change is a code diff on type, amount, components, plan or reason,
and the borrower must confirm it. Status checks, new evidence and duplicate messages
don't restart the flow.

```mermaid
stateDiagram-v2
  direction TB
  [*] --> DRAFT: agent creates vN
  DRAFT --> PENDING_APPROVER: context pack built
  PENDING_APPROVER --> PENDING_COMPLIANCE: approve
  PENDING_APPROVER --> REJECTED: reject
  PENDING_COMPLIANCE --> APPROVED: approve
  PENDING_COMPLIANCE --> REJECTED: reject
  APPROVED --> COMMUNICATED: approved template sent
  COMMUNICATED --> ACCEPTED
  COMMUNICATED --> EXPIRED
  DRAFT --> SUPERSEDED: material change confirmed
  PENDING_APPROVER --> SUPERSEDED: material change confirmed
  PENDING_COMPLIANCE --> SUPERSEDED: material change confirmed
  APPROVED --> SUPERSEDED: never sent
  COMMUNICATED --> SUPERSEDED: flag offer_outstanding
  PENDING_APPROVER --> WITHDRAWN: borrower withdraws
  SUPERSEDED --> [*]: vN+1 restarts at DRAFT
  REJECTED --> [*]
  ACCEPTED --> [*]
  EXPIRED --> [*]
  WITHDRAWN --> [*]
```

## 7. Supersession and stale-approval guard

The borrower changes ₹12,000 to ₹8,000. Version 1 is cancelled in one transaction, and
the approver's late click on v1 gets a 409. Version 2 then goes through the full flow
with a diff against v1. Each version runs on its own LangGraph thread
(`case:{id}:v{n}`), paused with `interrupt()` and resumed only through the
version-checked decision API.

```mermaid
sequenceDiagram
  autonumber
  actor B as Borrower
  participant AG as Agent (LangGraph)
  participant CS as Case service
  participant AP as Approval graph
  participant R as Approver
  participant C as Compliance
  B->>AG: "Waive penalty, settle for ₹12,000"
  AG->>CS: create case (account-scoped)
  CS->>AP: start thread case:WV-1:v1
  AP-->>R: interrupt → context pack v1
  AG-->>B: Ref WV-1 · under review (template)
  B->>AG: "Make it ₹8,000"
  AG->>AG: extract request · code diff → material
  AG-->>B: Change ₹12,000 → ₹8,000? Restarts review [Yes] [No]
  B->>AG: Yes
  AG->>CS: supersede v1 → v2
  Note over CS,AP: one DB transaction<br/>v1 SUPERSEDED · cancel tasks + thread<br/>create v2 with diff · notify reviewers
  CS->>AP: cancel thread v1 · start case:WV-1:v2
  R->>CS: approve v1 (etag v1) — stale click
  CS-->>R: 409 case_superseded → open v2
  AP-->>R: interrupt → context pack v2 (with diff)
  R->>CS: approve v2 (etag v2)
  CS->>AP: resume v2
  AP-->>C: interrupt → context pack + approver decision
  C->>CS: approve v2 (etag v2)
  CS-->>B: compliance-approved template with outcome
```

## 8. Evaluation and CI loop

Synthetic data feeds a dev set and a held-out set. Code-based checkers block any merge
that breaks a must-be-zero metric:

- disclosure before verification
- cross-account data
- commitments
- fake payments accepted
- numbers not backed by a tool result
- approvals on stale versions

Production signals flow back as regression cases.

```mermaid
flowchart LR
  GEN["data_gen — seeded synthetic<br/>D1 · D2 · D7 · policy · routing<br/>conversations · voice · screenshots"] --> DEV["Dev set — 300"]
  GEN --> HO["Held-out set — 300<br/>T3 only, never tuned on"]
  DEV --> H["Harness<br/>/v1/simulate/batch"]
  H --> CK["Checkers<br/>disclosure · cross-account · commitment<br/>fake accepted · grounding · stale approval"]
  CK --> LS["LangSmith experiment<br/>prompt version · model · per-slice scores"]
  LS --> GATE{"Any hard-zero<br/>regression?"}
  GATE -->|yes| BLOCK["Block merge"]
  GATE -->|no| MERGE["Merge → deploy"]
  MERGE --> PROD["Demo / prod traffic"]
  PROD --> FB["Signals: reassigned tickets · verdict overrides<br/>👍/👎 · repeat contact 72 h · alerts"]
  FB --> REG["Regression dataset"]
  REG --> H
  HO --> FINAL["Pre-demo scorecard"]
```

## Design vs. current code

The diagrams are the target. This table shows where the code in `src/servicing_agent/`
stands today, so each team can see what's left. ✅ matches, 🟡 partly there or named
differently, ⬜ not built yet.

| Area | Design (diagrams) | Code today | Owner |
|---|---|---|---|
| Ingress | Webhook enqueues to Redis Streams, returns 200 in under 100 ms | ⬜ Webhook and `/v1/simulate/messages` call the agent directly; no HMAC check | T2 |
| Queue, lock, batching | Redis Streams, per-conversation lock, 3 s batching | 🟡 In-process lock in `platform/conversations.py`; no queue or batching | T2 |
| Holding reply | Under 3 s for voice and image | ⬜ | T2 |
| Perception | Speech-to-text, image classification, VLM + OCR | ⬜ Stub: `/simulate` takes pre-extracted `receipt` / `transcript` | T3 |
| Graph nodes | normalize → screen → plan → verify → execute → compose → output_guard | 🟡 No separate normalize/compose nodes; replies come straight from templates in the handlers | T1 |
| Lanes and routing | Routine, payment claim, case flow, ticket | ✅ `agent/handlers.py` | T1 |
| Clarify once, then route | Yes | ✅ | T1 |
| Verification | Account number + active contact + OTP; uniform reply | ✅ `agent/verification.py` | T1 |
| Lockouts | 3 OTP tries; 5 failures per sender in 24 h; 3+ numbers failing on one account | 🟡 First two done; per-account cross-number check missing | T1 |
| SIM-swap flag | Contact added in the last 7 days → human check before docs and waivers | 🟡 Recorded on the `verified` event; not enforced | T1 |
| OTP and session lifetime | OTP valid 5 min; session idle 30 min, absolute 120 min | 🟡 Idle 30 min only | T1/T2 |
| Unverified screenshot | Store and extract now, reconcile after verification | ✅ Claim waits in `pending_tasks` until verified | T1 |
| Payment verdict names | PAID / EDITED / REUSED / NOT_FOUND | 🟡 `MATCHED` / `MISMATCH`, `OTHER_ACCOUNT`, `REPLAY` / `NOT_FOUND`, plus `PENDING` inside the settlement window | T3 |
| Not-found routing | Recon queue P3 | 🟡 `payment_reconciliation` is P2 in `config/routing_matrix.yaml` | T2 |
| Receipt source | Built from the D7 `txn_id` | 🟡 Built from the matched D7 transaction, keyed by UTR | T2 |
| Case states | DRAFT, PENDING_*, APPROVED, COMMUNICATED, ACCEPTED, EXPIRED, WITHDRAWN, SUPERSEDED | 🟡 No DRAFT, ACCEPTED, EXPIRED or WITHDRAWN yet; new versions start at `PENDING_APPROVER` | T2 |
| Supersede in one transaction, 409 on stale etag | Yes | ✅ `platform/cases.py`, tested with concurrent races | T2 |
| Approval graph per version | `case:{id}:v{n}` thread, `interrupt()` approver → compliance | ⬜ Decisions update the DB directly | T2 |
| Context pack | Source-linked, numbers verified | ✅ `platform/context_pack.py` (template-built) | T2 |
| Reviewer console, ops dashboard | Yes | ⬜ | T2 |
| Postgres, Redis, S3, LangSmith, Prometheus | Yes | 🟡 SQLite for cases and audit; everything else not yet wired | T2/T3 |
| Eval harness | `/v1/simulate/batch`, checkers, LangSmith, CI gate | 🟡 `evals/run_eval.py` runs in-process with the same zero-tolerance gates; no batch endpoint, LangSmith or CI yet | T3 |
| Voice and screenshot datasets | Yes | ⬜ Text-only scenarios | T3 |
| Concurrency target | 500 concurrent conversations (the page footer also says 750 users in section 8) | ⬜ Not load-tested | T2 |
