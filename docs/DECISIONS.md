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
