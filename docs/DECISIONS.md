# Decisions

## Settled

| Decision | Outcome |
|---|---|
| Authentication | Account number + sender's mobile is an active contact on that account + OTP step-up (OTP can be turned off by config). One account per session. |
| Customer with many numbers / shared numbers | Mobile is never a lookup key on its own; the account number decides which customer. |
| Access to other accounts / other details | Denied. Gateway injects the account scope; per-intent field allow-lists. |
| Changed waiver/settlement request | Borrower confirms, then the old version is superseded and the full approval flow restarts. Approvals on old versions get 409. |
| Approver / compliance context | Context pack per version, every number from source records. |
| Datasets | All synthetic, seeded, validated. |
| "Caught" fake screenshot | Never accepted + flagged to fraud review. Tamper forensics parked. |
| Dunning suppression | Parked. |

| Inbound menu: which loan to verify | After a menu pick, loans where the sender is an active contact are offered directly: 1 loan → OTP, several → masked chooser (first 4 + last 4), none → type the account number. An OTP is still issued only for a loan this number is registered on (krishnamurthyp, Nov 2026; demo scope) |

## Open (defaults in effect until decided)

| Decision | Default | Affects |
|---|---|---|
| Can a co-borrower verify on the account? | Yes, with an active `account_contacts` link; sees that account only | T1, T2 |
| Offer already sent, then the borrower changes the request | Cancel in the system, flag `offer_outstanding`; compliance decides how the withdrawal is communicated | T1, T2 |
| Approval order and authority levels | Approver → compliance; authority level from policy band (amount, DPD) | T2 |
| Does a correctly routed complaint count as "resolved without a human"? | No: counted as routed, not auto-resolved | T3 |

## Free-form questions, typos and internal info (2026-11-20)
Real WhatsApp testing showed "intrst rate", "collctr nme" and questions outside the keyword list
falling to "didn't get that" and then a ticket.
- **Typos**: the rules planner retries with domain-word spelling correction when it finds nothing.
- **Classifier fallback**: if the rules still find nothing, Sarvam (same key as the normalizer, `SA_NORMALIZER=sarvam`)
  labels the message. It only picks an intent; slots still come from code.
- **`account_question`**: other questions about the verified loan are answered by Sarvam from an allow-listed
  fact set (`config/intent_fields.yaml`). The draft goes through the output guard; if it fails, or no model is
  configured, a facts template is sent instead.
- **`restricted_info`**: collector/agency names, internal notes and bureau data stay off every allow-list. The
  borrower gets a polite decline (and an offer to raise a concern), not a ticket.
- **`out_of_scope`**: off-topic messages get a short "what I can help with" reply, not a ticket.

## Triage and the approval message (2026-11-20)
- Every executed turn gets a `triage` (audit event, normalized view, and on every ticket/case raised):
  `category` compliance|routine, `sentiment` distressed|frustrated|neutral|positive, `hardship`,
  `needs_approval` (waiver/settlement) and `priority` (high when a compliance item comes with distress or frustration).
  Hardship said before OTP verification is carried onto the parked tasks.
- Waiver/settlement asks are also caught without the words "waive"/"settle" ("reduce the amount I owe",
  "discount to close", "can't afford the full amount"); charge disputes ("reverse the extra charge") stay disputes.
- The borrower is told the approval path: supervisor review → compliance check → outcome on WhatsApp,
  nothing decided yet, keep paying EMIs. Status replies name the step (1 of 2 / 2 of 2). Hardship gets an
  acknowledgement and priority. Outcomes still go only through compliance-approved templates.
- Why rules, not the LLM, for triage and replies: these messages carry regulatory weight, must be the same every
  time, and must be auditable. The LLM is used where wording varies (classifying unclear messages, translating,
  answering free-form loan questions) and its output is checked in code.

## Unmatched payment claims: authenticity score and time-based reply (2026-11-20)
A claim that doesn't match the ledger is no longer answered with one generic "our team is reviewing it".
- **Authenticity score** (`agent/tools/authenticity.py`): a readable logistic model over ledger/loan features
  (UTR on this loan, amount/date vs ledger, payee, receipt status, amount fits EMI/overdue, extraction confidence).
  `supported` >= 0.6, `likely_fake` <= 0.2. It never accepts a payment; only an exact ledger match does.
  Weights are hand-set and can be refit on the eval labels.
- **Payment age** = message time minus receipt date + time (India time).
  - ≤ `SA_CLAIM_FRESH_MINUTES` (60) or unknown: "we'll check again in a couple of minutes"; the worker rechecks
    after `SA_CLAIM_RECHECK_SECONDS` (120). Matched then → confirmation + receipt; still unmatched → escalation.
  - Older, plausible and inside the settlement window → "can take up to a day".
  - Otherwise → escalation ticket (fraud_review if flagged or likely_fake, else payment_reconciliation) and the
    borrower gets the reference. The score, signals and age are on the ticket; the borrower is never accused.

## Genuine payments that haven't posted yet (2026-11-20)
A payment missing from the ledger is not evidence of a fake: "UTR not found" carries no penalty in the
score; only contradictions (edited amount/date on a real UTR, someone else's UTR, wrong payee, future date) do.
- **Supported** claims are rechecked on a back-off (`SA_CLAIM_RECHECK_DELAYS`, default 2 min, 15 min, 1 h, then every
  3 h) until they post (confirmation + receipt, automatically) or the settlement window (`SA_SETTLEMENT_WINDOW_HOURS`,
  24 h) runs out. Rechecks are silent; the borrower hears only the result.
- Borrower wording: within the hour "we'll check again in a couple of minutes"; older "it isn't showing yet, bank
  payments can take a few hours, no need to pay again"; at window end "we've asked our payments team to verify it
  directly with the bank" + reference (payment_reconciliation queue, not fraud review).
- **Not enough evidence** (e.g. a typed UTR only): one patient recheck, then the same bank-verification escalation.
- **Contradicting evidence**: fraud review (after one quick recheck if paid within the hour).
- Rechecks live in a Redis sorted set (`sa:claim_rechecks`, scored by due time) and survive restarts; each job carries
  the loan it was made on. ZREM claims a job so it runs once across workers. Without Redis: in-process timers.
- Next step (not built): ask the payment provider's transaction-status API for the UTR instead of waiting for the LMS.

## Real-time answers and borrower confirmation (2026-11-20, supersedes the recheck defaults above)
The demo is about accurate answers in real time, so the agent tells the borrower what it found now and asks
before acting, instead of promising follow-ups hours later.
- **Waiver / settlement**: the agent explains the approval path (supervisor → compliance → outcome on WhatsApp,
  keep paying EMIs) and asks "Shall I submit? YES / NO". Only YES creates the case and the approval ticket
  (`borrower_confirmed: true`). NO cancels; anything else lets the question lapse.
- **Payment discrepancy**: the reply says what didn't match, in plain words and without ledger values
  ("the amount on this receipt doesn't match our record of this payment", "this payment reference isn't linked to
  your loan", "we can't find this payment in our records yet"). It then asks whether to escalate: to the
  **compliance team** when the receipt contradicts the ledger (fraud_review), otherwise to the **payments team**
  (payment_reconciliation). YES raises the ticket with the authenticity score; NO invites another receipt/UTR.
- Every claim is still written to the claim ledger with its verdict and score, whatever the borrower answers.
- Background rechecks are now **off by default** (`SA_CLAIM_RECHECK_DELAYS` empty). Set it to re-enable silent
  rechecks that auto-confirm a payment that posts later.
- One general `pending_action` in the session (graph node `confirm_action`) handles both questions.

## Demo rules: 2-minute window, silent fraud escalation, waivers with a payment, policy retrieval (2026-11-20)
Supersedes the earlier "ask whether to escalate" and back-off recheck sections for payment claims.
- **Payment not in the LMS**: "I'll check again in 2 minutes" (`SA_CLAIM_WINDOW_SECONDS`, default 120, counted from
  when the borrower sent it). One recheck: posted → confirmation + receipt; still missing → "couldn't confirm,
  escalation filed" + reference (payment_reconciliation). Duplicate or late rechecks stay silent.
- **Contradicts the ledger or scores likely fake**: fraud_review ticket immediately, borrower told only "we couldn't
  verify this receipt, send another receipt or the UTR". Resends never open a second ticket and never reveal it.
- **Waiver / settlement sent with a payment** (claims run first in the turn):
  - payment not confirmed (not posted yet / unverified) → no request; "send it again once confirmed";
  - confirmed and covers the full amount due (overdue, else EMI) → "nothing left to waive", no request;
  - confirmed but partial → shortfall + consequences + YES/NO; on YES the case enters approval and a second ticket
    goes to `compliance_review` (`partial_payment_waiver`). Requested amount defaults to the shortfall.
- **Policy as a document** (`config/policy/servicing_policy.md`, `agent/policy.py`): approval stages, consequences,
  claim and escalation rules are clauses with IDs, tags and compliance-approved borrower lines. BM25 retrieval (with
  tag filters for the situation) picks the clauses; borrower lines are quoted verbatim, so the output guard still
  applies. New `policy_question` intent answers "how does approval work / will settlement affect my credit score".
- **Context carried forward**: every ticket gets a `context_bag` (clauses applied with text, policy version, triage,
  verified account, last 8 messages masked); the approval context pack adds `policy_clauses` and the bag; a
  `policy_applied` audit event lists the clauses per turn.
- Demo limitation: the LMS CSV is static, so the amount due does not drop after a confirmed payment.

## Wrong loan is not fraud; follow-ups refer to the last receipt (2026-11-20)
- A UTR on another loan where the same mobile is an active contact (policy CLM-05) is a wrong-loan pick: no fraud
  ticket. The borrower is told it belongs to their other loan (masked) and can reply SWITCH LOAN: chooser + code,
  then the receipt is rechecked on that loan automatically. Someone else's UTR is still CLM-03 (silent fraud).
- A payment claim with no UTR ("I already made payment", "I already shared the UTR") within 30 minutes of a receipt
  is answered about that receipt (confirmed / still checking / escalated reference / neutral "couldn't verify, send
  the UTR from your bank app or call the helpline"), instead of asking for the UTR again.

## Session context: payment evidence outlives the verified session (2026-11-20)
`platform/session_context.py`, policy clause CTX-01. Per conversation (WhatsApp number), the structured payment
evidence the borrower shared (UTR, amount, date, time, payee, status, source, loan, last outcome, ticket) is kept
for 24 h, at most 5 items; Redis on the live channel (survives restarts), in-memory for tests/simulator.
- "I already paid / already shared the UTR" with no UTR: the remembered evidence is re-checked against the ledger
  now, for the loan verified now (MATCHED -> confirmed; other own loan -> SWITCH LOAN; flagged -> neutral with the
  existing ticket; still in the window -> "still checking", no second window). Receipts are remembered as they
  arrive, even before verification.
- Follow-up questions ("what was the UTR I sent?") are answered from it (masked UTR), and the Sarvam answerer gets
  the same masked list as context.
- Guards: never a verdict (always re-checked), only read in verified turns, filtered to the loan verified now,
  structured fields only (no OCR/free text, so no injection carry-over), bounded and expiring.

## Option C: the policy document decides; hybrid retrieval explains; the model re-phrases (2026-11-20)
- **Decisions** come from `decides / when / then` lines in `config/policy/servicing_policy.md` (CLM-01/05/03/02 for
  payment claims, WVR-02/WVR-01/PPW-01/APR-03 for waiver/settlement requests; first match in document order wins).
  Code computes a fixed set of facts (verdict, authenticity, same_borrower_loan; request, payment, coverage) and acts
  on the matched clause's action. The 2-minute window, ticket types and silent-or-notify live in the document;
  unknown facts/actions fail at load. `SA_CLAIM_WINDOW_SECONDS` is only an optional override.
- **Retrieval** is hybrid: `0.5 * BM25 (normalised) + 0.5 * cosine` with `BAAI/bge-small-en-v1.5` via fastembed
  (local ONNX, ~9 ms per query; cached in `.cache/fastembed`). `SA_EMBEDDINGS=sarvam` uses Sarvam /v2/embeddings once
  the org has Provisioned Throughput access (not enabled today); `off` = BM25 only (tests, evals). Policy answers
  need a hybrid score >= 0.30 (off-topic ~0.25); each search's scores go to the audit log (`policy.search`).
- **Decision record** on every ticket (context bag): clause, action, facts, policy version, a one-line reviewer note
  and the masked context window (payments, screened voice snippets, requests, decisions).
- **Model-written replies** (`agent/reply_writer.py`, Sarvam): only whitelisted templates, never silent-escalation
  ones. Used only if every number/reference/masked account is kept, no number or reply instruction (YES/NO/SWITCH
  LOAN) is added, no review-revealing words, and the output guard passes; otherwise the template is sent. Live
  testing caught the model adding "reply YES or NO" to messages without a question; the instruction-word check
  blocks that.
- Not done (deliberately): vector DB, the model deciding actions, a model-written reviewer summary (the reviewer
  note is built from the decision record).

## Two verification paths (deliberate split)

There are two OTP flows, by channel, and this is intentional (not duplication to remove):

- **Agent-OTP** (`agent/verification.py`): the agent's own verify subflow (account → OTP → bind
  `AuthScope`), using `tools.otp`. This serves `/v1/simulate/*`, `/v1/turns/batched` and the test
  suite — i.e. the deterministic eval/demo path. In offline/dev it accepts `SA_OTP_STUB_CODE`.
- **Email-OTP + loan chooser** (`platform/inbound.py` `InboundFlow`): the live WhatsApp webhook
  path (greeting → menu → masked loan chooser → emailed OTP via `RedisOtpService` → bind scope).
  `platform/api/app.py` hands the agent's `ask_account` reply over to this menu flow via
  `graph.py::hand_verification_to_menu`. Dev routes every OTP email to `SA_OTP_EMAIL_OVERRIDE`.

Both bind the same `AuthScope` and share one `RedisOtpService` (the agent path wraps it with
`AgentOtpAdapter` on a live channel, chosen by `otp_mode`/channel in `runtime.py`). **For the live
demo, drive it through the WhatsApp webhook (email-OTP) and make sure the override inbox is
reachable; for scripted/eval runs use `/v1/simulate` (agent-OTP, stub code).** Do not demo both.

## Session-scoped context window; dedup and hard stops (2026-11-20)

**Context window per verified session.** `SessionContextStore` is keyed by
`conversation:loan:verified_at`. A new verification, a loan switch or an expiry starts an empty window, and the
window is deleted when the session expires. It holds voice snippets, masked receipts, requests, decisions and
escalations. It feeds the reply writer and the ticket context bag. Nothing from an earlier session is carried
into a new one.

**Payment evidence follows the loan, not the chat.** "I already paid" in a new session reads the latest claim on
the verified loan (last 24 h) from the `payment_claims` ledger, which already records every claim. That claim is
re-checked against the ledger now (CTX-01). Rechecks and these lookups don't count as resend attempts.

**The same receipt again (DUP-01, `max_resends=3`).**
- While a check is running: the borrower is told how long is left, and no second check is scheduled.
- After escalation: the existing reference is returned and no second ticket is raised. After a silent CLM-03
  review the reply stays neutral.
- After `max_resends` sends: a hard stop. The receipt isn't re-processed, the attempt is logged
  (`claims.resend_limit`) and the borrower is told it's already with the team.
- A receipt that now matches the ledger is still confirmed.

A "please escalate" request is deduplicated on the open `borrower_escalation` ticket for the loan.

**A request already in approval (DUP-02, `max_revisions=2`).**
- The same ask again gets the reference and current stage (`case_active`).
- A different ask is confirmed first: the current request is cancelled and approval restarts from step 1. YES
  re-initiates it; NO keeps the current request. The last allowed change is flagged as the last.
- After `max_revisions` changes: a hard stop (`case_revision_limit`, `cases.revision_limit` event) and a pointer
  to the helpline.

Both limits are `then:` parameters in the policy document, so changing them is a document edit.

## Request restarts, SIM-swap check, idempotent sends (2026-11-20)

**A restarted request retires its old tickets.** When the borrower confirms a change (DUP-02), the old
version's open tickets (approval, compliance) are set to `superseded`, with a note. They drop out of the
reviewer queue and aren't counted as resolved. The same ticket types are opened for the new version, carrying
`version`, `diff` and `supersedes`. A decision on the old version still gets a 409.

**SIM-swap check (SEC-01, `recent_days=7`).** It is checked when used, from the verified `contact_id`, so it
covers both verification paths.
- If the mobile was added within `recent_days`, documents (statement, receipt, closure quote) are held behind a
  `sim_swap_review` ticket (security queue, P1). One open review covers later requests.
- A waiver or settlement is still raised after YES, but carries `security_check` and a linked security ticket.
- Answers about the loan are unaffected.

**Each reply is sent at most once.**
- A send ledger (Redis `SET NX`, 24 h) has one key per conversation, turn and reply. A webhook redelivery,
  queue recovery or duplicate recheck sends only replies that never went out.
- A retry happens only when the message certainly wasn't delivered (connection error, 429 or 5xx).
- A timeout is logged as `unknown` and not resent, to avoid a double message. A 4xx releases the key, so a
  redelivery can send it.

## Escalation is confirm-first; borrower lines are general (2026-11-20)

**The bug.** A question about escalation was answered with ESC-02's borrower line, "I've passed this to our
compliance team; you'll get a reference…". Policy answers quote borrower lines word for word, so a line written
as a confirmation told the borrower something had been done when nothing had: no ticket, no reference.

**Fixes.**
- **Borrower lines are general explanations**, true in any conversation (ESC-02, CLM-05, DUP-01, DUP-02 and
  SEC-01 were rewritten). A test fails if a line claims an action ("I've passed…", "we already have…").
- **Escalation asks first**, like waivers. "Escalate this" now gets "Do you want me to escalate this … Reply YES
  or NO". YES raises the `compliance_review` ticket, carrying the borrower's original words. NO, or moving on,
  raises nothing. An already-open escalation still returns its reference without asking.
- **The Monitor shows voice notes** as their transcript (with the message type), not "(no text)".

## Can't-pay conversations: follow-ups, part payments, "to be paid" (2026-11-20)

These fixes come from a live WhatsApp session.

- **"What is my total outstanding which needs to be paid?" was also read as a payment claim**, so the agent asked
  for a UTR. Fix: "be paid" and "been paid" no longer count as claims.
- **"tell me" after the can't-pay options was out of scope.** Fix: the session keeps the turn's topic
  (`last_topic`). A short follow-up ("tell me", "explain", "what does that mean") right after a can't-pay,
  waiver, settlement, escalation or policy reply gets that topic's approved policy lines (hybrid search, most
  relevant first) and the next step. With no such topic, it's read as usual.
- **"I can only pay ₹5000" got the same options again (three times).** Fix: an amount below what's due gets a
  payment link for that amount, the shortfall, and an offer to ask for the rest to be waived or settled. A
  following WAIVER or SETTLE defaults to that shortfall, then goes through the usual confirmation (consequences,
  YES). An amount covering the dues is a normal payment link. New rules also catch "can only pay" and "don't have
  much money".
- **Can't pay is triaged as compliance**, and as high priority when distressed, because it may become a waiver or
  settlement.

## Waiver and settlement details agreed before YES (APR-04, 2026-11-20)

**The bug.** "Why does the waiver and settlement need approval?" was read as a settlement request, because the
word "settlement" matched. The next "Yes, I'd like a settlement" then submitted a case with no amount and no
reason.

**Fixes.**
- **"Why … approval / supervisor / review / compliance" is a policy question.** It's answered, not raised.
- **Details come first.** Before the YES/NO confirmation, the agent agrees the details in one message:
  - for a settlement, the amount the borrower offers to pay;
  - for a waiver, the amount or the charges to waive;
  - the reason: a known category (job loss, medical, business loss, salary delay, reduced income, family
    emergency, financial difficulty) or the borrower's own words, recorded as "other".
- **Answering.** The answer can be a bare "8000", "₹5,000, I lost my job", and so on. A plain "yes" without the
  details re-asks. After two unanswered asks nothing is raised. NO cancels. A different question lets the
  request lapse.
- **Settlement amount.** It is what the borrower pays ("a settlement where you pay ₹X"), so "I can only pay
  ₹5,000" followed by SETTLE offers ₹5,000. A waiver after that covers the shortfall.
- **Hardship.** It's acknowledged when the details are asked. The first message's triage (hardship, high
  priority) is kept on the ticket even though the confirmation comes a turn later.
- **Tests and eval.** These now include a reason in their request messages.

## Withdrawing or restarting a request; session limits (2026-11-20)

These come from a live session.

**Withdraw.** "Terminate the whole thing and reinitiate a new flow for ₹1615" was out of scope, because the agent
had no way to cancel a request. There is now a `case_withdraw` intent (rules and classifier label):
- **"Cancel / withdraw my request":** confirm first (YES / NO). YES sets the case to `WITHDRAWN` and closes its
  open tickets. NO keeps it.
- **"Cancel it and start a new one for ₹X" (or "start a new waiver"):** handled as a change of the open request
  (DUP-02). The type is what they said, else the type just discussed in this session, else the open case's type.
  It goes through the details check (APR-04) and the restart confirmation. With no open request, it's a new
  request.

**Other fixes from the same session.**
- "Tell me more about my previous request" gives the request's details (type, amount, reason) and stage.
- A change to a request also needs its amount and reason. Older requests without a reason are asked for one first.
- "My outstanding and the EMI to be paid this month" answers both.

**Session limits.** The verified session slid forward by 30 minutes on every message and had no absolute cap, so
an active chat never expired. Now:
- the idle window is `SA_SESSION_IDLE_MINUTES` (default 30);
- the absolute limit is `SA_SESSION_MAX_MINUTES` (default 120) after verifying, which activity can't extend.

Both verification paths use the same settings.

## Hardship ladder: waivers and settlements as a last resort (2026-11-20)

**Goal.** Don't offer a waiver or settlement up front. First understand the borrower and make them aware of the
consequences; only then, per policy, offer a waiver; a settlement is the worst case.

**How it's decided.** The policy document decides, through the `hardship` situation:

| Order | Clause | When | Action |
|---|---|---|---|
| 1 | HRD-07 | `nudges_given >= 3` and the borrower refuses to pay at all or keeps asking to settle | offer settlement |
| 2 | WVR-03 | ≥70% paid, 0-10 days past due, recognised reason, 2 rounds of encouragement done | offer waiver |
| 3 | WVR-04 | same, 10-20 days past due, after 3 rounds | offer waiver |
| 4 | HRD-08 | 3 rounds done (and no waiver) | offer settlement |
| 5 | HRD-03 | otherwise | encourage payment |

**Facts (computed by code).**
- `nudges_given`: per loan, kept 7 days.
- `stance`: unable_now, pay_part, ask_waiver, ask_settlement or refuse_all.
- `paid_share`: confirmed payments in the last 30 days ÷ current overdue amount.
- `dpd_band`: a new accounts column (0-10, 10-20, …), derived from `dpd`.
- `reason_known`: a recognised hardship reason.

The rule language gained `>=`, `<=`, `>`, `<`.

**Replies.**
- **Encouragement:** three variants, empathetic, with the overdue amount, the due date and the credit-score
  consequence, plus a part-payment link on request. These replies may be reworded for tone; the facts are fixed.
- **A part payment offer:** a payment link straight away; no waiver or settlement talk.
- **A direct "waiver" / "settle" before the ladder allows it:** "A waiver or settlement is a last resort…" plus
  encouragement.
- **Once offered:** the request goes through APR-04 (amount and reason, prefilled from the ladder), the
  consequences and YES.

**Switch.** `SA_HARDSHIP_LADDER` is on by default. The unit-test fixture and the eval harness run with it off,
because they exercise the request mechanics directly; `tests/test_hardship_ladder.py` covers the ladder.

**Demo data.** CN8677973003 is set to 15 days past due (band 10-20), with a confirmed ₹12,500 payment (72% of
₹17,361.48) on 2026-11-18, so the waiver branch can be shown at the last round.

## Pending YES / NO questions lapse (2026-11-20)

**The bug.** At 11:14, "waiver" asked "change your request? YES / NO". It was never answered. At 12:10, "No, I
am in a very difficult situation. I can't pay any amount right now" was taken as the answer, and the agent
replied "we'll keep your earlier request".

**Fixes.**
- **Pending questions now lapse:** both a pending change and a pending action (submit, escalate, withdraw).
  - **After 15 minutes:** the question expires and nothing is done.
  - **When the borrower moves on:** if the next message isn't an answer, the question is dropped.
- **What counts as an answer:**
  - a short yes / no (six words or fewer), or
  - a longer message starting with yes / no whose own topic is the one asked about, or which has no other topic
    ("Yes, I'd like to request the settlement").
- **Anything else is a new message,** so "No, … I can't pay any amount" continues the hardship ladder.

## Hardship ladder v2: waiver, then pre-approved settlement or collections (2026-11-20)

**Why.** In live testing (12:15–12:17) the agent went straight to a settlement after three rounds. The new order is:

1. **Three rounds of encouraging payment** (HRD-03).
2. **The waiver,** if eligible:
   - WVR-03: ≥70% of the overdue amount paid, 0-10 days past due, a recognised reason, after 2 rounds;
   - WVR-04: the same at 10-20 days past due, after 3 rounds.
3. **The waiver is offered again once.** After **two declines**, or when the waiver isn't available:
   - **Pre-approved settlement (STL-03),** only if the loan is pre-approved: a one-time amount, `amount_pct` (0.6)
     of principal outstanding plus overdue. The **payment link is sent immediately**, with the credit-bureau
     consequence. A `preapproved_settlement` ticket goes to the collections queue. No approval case is raised.
   - **Otherwise, the collections team (STL-04):** a `collections_review` ticket, and the borrower is told the team
     will reach out. The agent never offers or negotiates a settlement that isn't pre-approved.
4. **Instalments for the settlement** ("can I pay it in parts?") go to the collections team (STL-07).

**What makes a loan pre-approved.** The policy decides it in the new `settlement_preapproval` situation:
- **STL-05:** `accounts.preapproved_settlement = Y`, a new column. It's seeded Y where ≥90% of the principal has
  been recovered (39 accounts).
- **STL-06:** recovery ≥ 90% within the configured bucket. The demo uses 0-10 days past due; production would
  typically use bucket 3. The bucket and threshold are document edits.

**Waiver eligibility** is now its own situation, `waiver_eligibility`, so the `hardship` clauses read its result.

**Also fixed.** On a can't-pay message, the model's extra ticket-raising labels (charge reversal, dispute,
escalation, complaint) are dropped. The 12:15 charges ticket and the 12:17 escalation question came from these.
Explicit words for them are still caught by the rules, which run first.

**Demo loans (8867635479).**

| Loan | State | Path after the rounds |
|---|---|---|
| CN8677973003 | 10-20 days past due, 72% paid, not pre-approved | waiver; 2 declines → collections |
| CN9248665532 | 98% recovered, pre-approved Y | one-time settlement with link |
| CN5432712619 | 320+ days past due, pre-approved N | collections |

## Loan data from Postgres (SA_DATA_SOURCE=db, 2026-11-20)

**Before.** The agent read accounts, contacts and payments from the CSVs in `data/generated`, loaded at startup.
The Postgres tables were a copy, so editing them changed nothing.

**Now.** `DbDataStore` reads the same tables from Postgres and gives the same shapes, so the mock LMS, payment
feed and verification are unchanged.
- **Live edits:** it re-reads when the data is older than `SA_DATA_REFRESH_SECONDS` (default 5), so an edit in
  Postgres applies to the next message without a restart.
- **Postgres down:** it keeps serving the last good copy.
- **Mobile numbers:** `contacts` keeps the `mobile` alongside `mobile_hash`, because the agent needs the number to
  match senders and to send (demo scope).
- **Defaults:** `csv` stays the default (tests, evals, offline); the local `.env` sets `db`.

**Watch out.** `make db-load` replaces these tables from the CSVs; only the contact emails are kept. Re-running it
undoes manual edits made in Postgres.

## Logout, force logout, and "which loan am I on?" (2026-11-20)

**Ending a session.** `Runtime.end_session(conversation_id, reason)` removes everything tied to the session:
the scope, verification, pending YES/NO questions and details, the menu stage, the replayed question and the
session's context window. Payment claims, tickets and cases stay, because they belong to the loan. It records a
`session_ended` event with the reason.
- **Borrower:** "logout", "log out", "sign out" or "end session" works at any stage (menu, code, verified) and
  replies "You're logged out of loan CN89••••4971…".
- **Ops:** `POST /admin/api/conversations/{id}/logout?notify=true`, which is the **Force logout** button in the
  console's Conversations panel. With `notify`, the borrower is told their session was ended by the team.

**Which loan.** The `session_info` intent ("which account am I logged into?", "what is my current account
number", "am I still verified") replies with:
- the masked loan and its product;
- when the session ends, in IST (the earlier of the idle expiry and the absolute limit);
- how to switch loan or log out.

It never starts verification. Unverified, it says so. The console lists each conversation's active loan and
expiry.

**Tests and evals.** The `offline()` settings now force `data_source=csv`, so they don't pick up a local
`SA_DATA_SOURCE=db`.

## Waiver = pay the overdue amount minus charges, only at 0-10 DPD; demo payment page (2026-11-20)

**Rule (WVR-03 / WVR-05).** Only loans **0-10 days past due**, after three encouragement rounds, and only when
there are charges (bounce + penal) to waive. The 70%-paid and reason conditions are gone.
- **The offer:** "pay ₹(overdue − charges) now and we'll ask for the charges of ₹(charges) to be waived", with
  YES / NO.
- **YES:** a payment link tagged for this purpose.
- **NO:** a decline; the offer is made once more, then the ladder moves on to the pre-approved settlement or
  collections.

**Payment received.** The flow completes when the payment arrives, either through the payment page or through a
receipt / UTR matching the ledger. The borrower then gets:
- "we've received your payment…" with the receipt PDF;
- the waiver of the charges raised as an approval case (supervisor, then compliance) with its reference. It's
  raised once; a short payment says the waiver isn't started yet.

**Demo payment page.** Payment links point to `{public URL}/pay/{id}` (`SA_PUBLIC_BASE_URL`, or the
`NGROK_DOMAIN`).
- **The page:** shows the loan and amount. **Pay** writes a successful UPI payment to the ledger (CSV store in
  memory, or Postgres `payment_transactions` with `SA_DATA_SOURCE=db`), then sends the WhatsApp confirmation.
- **Paying twice** records nothing new.

**Why-ineligible text.** It now reads the thresholds from the clauses that exist (here: days past due ≤ 10), and
says so when there are no charges to waive.

**Demo.** CN8677973003 is set to 8 days past due (band 0-10) with ₹2,811.69 in charges and ₹17,361.48 overdue,
so the offer is to pay ₹14,549.79 and have ₹2,811.69 waived.

## Waiver v3: 10-30 DPD, approval first, same-day payment (2026-11-20)

**Builds on krishnamurthyp's `PayLinkService`.** Its database-backed links, `/pay/{id}` page, ledger transaction,
overdue deduction and receipt are reused. My earlier duplicate payment page was removed in the merge.

**Rule (WVR-03 / WVR-05).**
- **Eligibility:** 10-30 days past due (bands 10-20, 20-30), after three encouragement rounds, and there must be
  charges to waive.
- **The offer:** the agent explains that the charges come off the overdue after approval, that the rest must be
  paid the same day it's approved, and that otherwise the waiver is withdrawn. YES / NO.

**The flow.**
1. **YES:** a waiver case for the charges, `plan=pay_same_day`, goes to the supervisor, then compliance. No
   payment yet.
2. **Final approval:** instead of the generic "approved" text, the borrower gets a link for (overdue − charges),
   valid until 23:59 IST that day. It's a `pay_links` row with `purpose=approved_waiver`, `meta` (case, charges)
   and `expires_at`.
3. **Paid in time:** the overdue goes to 0, bounce and penal charges to 0, days past due to 0. The receipt and a
   "charges waived" message are sent.
4. **Not paid:** the server's minute loop (`expire_due`) expires the link and tells the borrower the waiver was
   withdrawn. The overdue, including the charges, stays, and a late payment on the link is refused.

**Stores.** Payment-service changes go through `store.update_account`, which writes to Postgres for the DB store.
Its startup replay is skipped for a persistent store, so deductions aren't applied twice.
