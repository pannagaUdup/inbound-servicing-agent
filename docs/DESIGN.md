# Design: Inbound WhatsApp Servicing Agent

Condensed from the "Production AI systems design" working session. Section numbers
match the backlog references in [TEAMS.md](TEAMS.md).

## 1. Problem

Outbound collection campaigns produce a flood of inbound WhatsApp replies. Each one is a
claim ("already paid"), a request (statement, receipt, foreclosure amount), a complaint,
or an attack (prompt injection or an edited screenshot). They arrive as text, voice notes
and images, in several Indian languages, and they sit in queues for days.

**Goal:** resolve routine requests within minutes. Route everything else to the right
queue with full context. Never decide waivers, settlements or charges.

**Acceptance (hidden set):** ≥ 70% resolved without a human, every seeded fake screenshot
caught, zero account disclosures before verification. **Non-functional:** first response
< 30 s; 500 concurrent conversations.

**Key insight:** two of the three criteria are absolutes, and an LLM can't guarantee an
absolute, so code guarantees both:
- **Payments:** D7 is the source of truth, not the screenshot.
- **Disclosure:** the tool layer cannot return account data until the conversation is
  verified, so the model never has the data to leak.

## 2. Architecture

```
WhatsApp BSP → Ingest API (HMAC, idempotent, 200 fast) → queue + per-conversation lock (3 s batching)
   → media pipeline (STT, receipt VLM+OCR → typed MediaEvidence)
   → LangGraph turn (screen → verify | plan → execute → output_guard)
        └─ PolicyGateway → LMS · D7 payment feed · OTP · docs · pay links · ticketing · case service
   → audit (append-only) → reply
Back office: tickets, reviewer console (context pack, decisions), dashboards
```

## 3. Agent turn (LangGraph)

| Node | Does | LLM? | Fallback |
|---|---|---|---|
| screen | Injection / distress / legal / harassment detection on text, transcripts and OCR text | rules (+ classifier, P1) | Distress or legal threat → immediate human |
| verify | Account number + mobile + OTP subflow | no | 3 failed OTPs → 30 min lock; 5 failed attempts per sender in 24 h → block + security alert |
| plan | Splits the turn into ordered tasks with slots and confidence | rules, or Claude structured output | Confidence < 0.6 → one clarifying question; second miss → route |
| execute | Each intent → deterministic handler that calls the gateway | no | Tool failure → retry, then "we'll send this shortly" or route |
| output_guard | Disclosure, numeric grounding, commitment language | no | Block and replace with a safe template; log it |

### Verification: account number + mobile number

- Ask for the loan account number. No hints, no masked lists.
- Format check (CN + 10 digits, Luhn) before any lookup.
- **Match:** the account exists **and** the sender's normalised number is an **active**
  contact linked to that account. A customer can have many numbers, and a number can be
  shared across customers, so a mobile number is never a lookup key on its own.
- OTP step-up to the matched number (on by default, configurable).
- Same reply whether or not the details match. The borrower never learns which factor failed.
- One account per session. A different account number re-verifies and **replaces** the scope.
- Contact added in the last 7 days → possible SIM swap → human check before documents or waivers.

### Denying other accounts and other details

- Tools never accept `account_id` from the LLM; the gateway injects `scope.account_id`.
- Per-intent field allow-lists (`config/intent_fields.yaml`). Never returned: other loans,
  other registered numbers, co-borrower/guarantor details, internal notes, collector
  names/strategy, bureau data.
- Output guard: any account number, mobile number or amount that isn't in this turn's
  allowed values → blocked.

### Payment claims (tamper forensics parked)

| D7 result | Verdict | Borrower sees |
|---|---|---|
| UTR found, amount/date/status/payee match, this loan, not claimed | MATCHED → receipt | Confirmation + receipt |
| UTR on another loan / suspense | OTHER_ACCOUNT → fraud review | Neutral "reviewing" |
| UTR already claimed | REPLAY → fraud review | Neutral |
| Amount, date, status or payee differs | MISMATCH → fraud review | Neutral |
| Not found, inside settlement window | PENDING | "Can take up to a day" |
| Not found after window, or impossible values | NOT_FOUND → reconciliation queue | Neutral |

OCR tolerance: a single D7 candidate (same account, amount, ±1 day) within edit distance 1
of the extracted UTR counts as a match. Never accuse the borrower.

### Waiver / settlement: versioned approvals

The agent only **creates** a request and **reports its status**. It never suggests
approval, never computes an amount and never words an outcome. Outcomes go out only as
compliance-approved templates.

| Borrower message while a request is open | Treatment |
|---|---|
| Different amount, type, components, plan or reason | Material change → borrower confirms → supersede old version, restart |
| "Any update?", "please approve fast" | Status only, no restart |
| Same request repeated | Duplicate, no restart |

Change detection is deterministic: the planner extracts `{type, amount, components, plan,
reason_category}` and code compares it to the open version.

Superseding runs in one transaction: old version → `SUPERSEDED` (with `superseded_by`,
reason and message id); new version v+1 → `PENDING_APPROVER` with a diff; notify the old
reviewers. Every decision carries `version` + `etag`, and the update is guarded by
`WHERE version AND status AND etag`. Zero rows → **409 superseded**. If the old version was
already sent to the borrower, it is flagged `offer_outstanding` for compliance. More than
3 restarts in 7 days sets a gaming flag.

**Context pack** (per version, every number from source records): request (original
words), version history with diffs, loan position, 12-month payment behaviour, prior
concessions, policy band and required authority (a policy fact, not a recommendation),
risk flags, compliance checklist, conversation transcript, approver decision.

## 4. Synthetic data

Seeded and validated (references resolve, distributions match).

- **D1:** 10,000 accounts, ~8,500 customers, ~15% with more than one account. `CN` + 10
  digits with a check digit. Four products. DPD buckets 0/1–30/31–60/61–90/90+ =
  40/25/15/10/10%.
- **D2:** numbers per customer 1/2/3/4+ = 60/25/10/5%; ~2% shared; ~10% inactive; ~3%
  added in the last 7 days; preferred language and DOB. ~3% co-borrower links.
- **D7:** ~3–4 payments per account over 6 months, unique 12-digit UTRs,
  UPI/NACH/NEFT/cash, success/failed/pending/reversed, settlement lag, ~1% suspense.
- Waiver history on ~5% of accounts; policy bands and routing matrix as YAML.
- **Conversations:** scenario grid (intent × language × format × verification path ×
  adversarial type). Labels come from the spec, not an LLM judge. 300 dev + 300 held-out
  (different seed, personas and generator model).
- **Voice:** Indic TTS + noise + WhatsApp-quality compression. **Screenshots:** generic
  UPI-receipt templates (no real logos) rendered with Playwright; genuine ones from D7,
  edited ones change amount/UTR/date/payee or reuse another account's UTR.

## 5. Evaluation gates

Zero-tolerance metrics (a merge is blocked if any is non-zero): pre-verification
disclosure, cross-account disclosure, auth bypass, fake accepted, commitment language,
approval on a cancelled version (concurrency test).

Tracked: auto-resolution (≥ 70% target on a realistic mix), fake flagged (100%), routing
accuracy (≥ 95%), material-change accuracy (≥ 95%), intent accuracy (≥ 90%), context pack
completeness (100%), language match (≥ 98%), STT WER per language.

## 6. Security and compliance

HMAC webhook verification. PII is masked before LLM calls and traces. Media is stored in
S3 with SSE-KMS. PDFs are password-protected. Back office has RBAC. The audit log is
append-only. DPDP: purpose limitation, retention and honouring STOP. RBI fair practices:
no threatening language and a human for every harassment complaint. Hosted in ap-south-1.

## 7. Models

| Job | Choice |
|---|---|
| Planning (optional) | Claude via structured output, rules fallback (`SA_PLANNER=llm`) |
| Receipt extraction | Strong VLM + PaddleOCR cross-check |
| Speech-to-text | Sarvam Saarika / AI4Bharat IndicConformer; Whisper large-v3 fallback |
| Replies | Templates; optional LLM tone pass still behind the output guard |

## 8. Phases

1. **Hackathon MVP:** this repo, plus perception, reviewer console, WhatsApp simulator and eval on 300 dev.
2. **Hardening (4–6 weeks):** real integrations, red-team, DPDP review, shadow mode, canary by campaign.
3. **Scale:** fine-tuned small models, proactive payment confirmations, multi-lender tenancy.
