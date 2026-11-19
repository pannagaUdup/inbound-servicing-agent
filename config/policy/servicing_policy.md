# Servicing policy: approvals, payment claims and escalations

Version: 2026-11-20 (demo). Owner: Compliance.

Each clause has an ID, tags used for retrieval, an optional **borrower** line (the only wording the agent
may send to a borrower for this clause, approved by compliance) and the internal rule. The agent retrieves
clauses for every approval or escalation, quotes the borrower lines word for word, and attaches the clause
IDs and text to the ticket and the approval context pack.

## APR-01 · Approval stages for waivers and settlements
tags: waiver, settlement, approval, stages, supervisor, compliance, process, consequence, how long, review
borrower: It isn't automatic: a supervisor reviews it first, then our compliance team checks it.
Every waiver or settlement request is a case. Stage 1: a supervisor (approver) reviews the request with the
context pack. Stage 2: compliance checks the supervisor's decision. Only then is the outcome sent to the
borrower, using a compliance-approved outcome template. The agent never states or implies an outcome.

## APR-02 · No outcome is promised; dues remain payable
tags: waiver, settlement, approval, consequence, rejected, approved in part, late charges, emi, what happens
borrower: It may be approved in part or rejected. Until there's a decision, the amount stays due and late charges may continue, so please keep paying your EMIs.
Requests can be approved in full, in part, or rejected. Until a decision is communicated, all dues remain
payable and normal charges continue to accrue. The agent must not say or suggest that a request will be approved.

## APR-03 · Borrower confirmation before submission
tags: waiver, settlement, approval, confirm, submit, consent
The agent explains the applicable clauses (APR-01, APR-02, and STL-01 for settlements) and submits the
request only after the borrower replies YES. A NO, or moving on to another topic, cancels the proposal.

## STL-01 · Credit bureau reporting of settlements
tags: settlement, credit, bureau, cibil, credit score, report, consequence, what happens, affect
borrower: If a settlement is approved, your loan may be reported to credit bureaus as "settled", which can lower your credit score.
A settled account is reported to credit bureaus as "settled" rather than "closed". This must be disclosed
before a settlement request is submitted.

## WVR-01 · Waiver after a confirmed full payment
tags: waiver, settlement, payment, full, screenshot, matched, not needed
When a waiver or settlement is requested together with a payment that matches the ledger and covers the full
amount due (the overdue amount, or the EMI when nothing is overdue), no request is raised: there is nothing
left to waive or settle.

## PPW-01 · Waiver or settlement after a partial payment
tags: waiver, settlement, payment, partial, shortfall, compliance, escalation
When the payment shared matches the ledger but is less than the amount due, the borrower is shown the
shortfall and the applicable clauses. On YES the case enters the approval stages (APR-01) and compliance
review is notified at submission (queue compliance_review), not only at stage 2. The requested amount
defaults to the shortfall unless the borrower states an amount.

## WVR-02 · Requests with an unconfirmed payment wait
tags: waiver, settlement, payment, unconfirmed, pending, unverified
If the payment shared with a waiver or settlement request is not yet confirmed (not in the ledger, or could
not be verified), no request is raised. The borrower is asked to send the request again once the payment is
confirmed.

## CLM-01 · Payment confirmation needs an exact ledger match
tags: payment, claim, receipt, screenshot, utr, confirm, ledger, lms
A payment is confirmed only when the UTR, amount, date, payee and status match the ledger (D7). A screenshot
or a typed UTR alone never confirms a payment. The receipt PDF is issued on confirmation.

## CLM-02 · Reflection window, then escalation
tags: payment, claim, not reflected, missing, pending, window, escalation, payments team, reconciliation
borrower: Payments can take a short while to show in our records; if yours doesn't, we'll escalate it to our payments team and give you a reference.
A payment not yet in the ledger is rechecked once when the reflection window ends (demo: 2 minutes from when
the borrower sent it). Still missing: the claim is escalated to payment reconciliation and the borrower is
told it couldn't be confirmed, with the ticket reference.

## CLM-03 · Suspected tampering or fake receipts
tags: payment, claim, fake, tamper, edited, mismatch, other account, replay, fraud, escalation, silent
A receipt that contradicts the ledger (edited amount or date on a real UTR, another loan's UTR, a reused UTR,
wrong payee, future date) or scores as likely fake is escalated to fraud review immediately. The borrower is
NOT told that the receipt was flagged or escalated; they receive a neutral message asking for another receipt
or the UTR. Resending the same receipt does not open a second ticket.

## CLM-04 · Neutral language and no ledger disclosure
tags: payment, claim, communication, accuse, disclose, neutral
The agent never accuses the borrower and never discloses ledger values (amounts, dates, other accounts) when a
claim does not match.

## HRD-01 · Hardship
tags: hardship, job loss, medical, distress, priority, waiver, settlement
When the borrower mentions hardship, the agent acknowledges it and the request is marked high priority for
the approval queue. Hardship does not change the approval stages.

## PRV-01 · Internal information
tags: collector, agency, internal, credit score, bureau data, privacy
Collector or agency assignments, internal notes and bureau data are never shared on WhatsApp. The borrower is
told politely and offered help with their own loan details.

## ESC-01 · Context carried with every escalation
tags: escalation, ticket, context, approval, compliance, audit
Every ticket and approval case carries a context bag: the clauses applied, the triage (category, sentiment,
hardship, priority), the payment evidence and authenticity score, and the recent conversation (masked). The
approval context pack includes the same clauses so reviewers apply the same policy the agent applied.
