# Servicing policy: approvals, payment claims and escalations

Version: 2026-11-20 (demo). Owner: Compliance.

Each clause has an ID, tags used for retrieval, an optional **borrower** line (the only wording the agent
may send to a borrower for this clause, approved by compliance) and the internal rule. The agent retrieves
clauses for every approval or escalation, quotes the borrower lines word for word, and attaches the clause
IDs and text to the ticket and the approval context pack.

**Decision clauses** also carry three machine-readable lines, and these decide what the agent does:

* `decides:` the situation the clause decides (`payment_claim` or `case_request`);
* `when:` the condition, over facts the agent computes (several `when:` lines mean "any of these");
* `then:` the action, as `key=value; key=value`.

Within one situation the **first matching clause in this document wins**, so order matters. Changing a
threshold, a queue or whether the borrower is told is an edit here, not a code change.

Facts for `payment_claim`: `verdict` (MATCHED, ALREADY_CONFIRMED, PENDING, NOT_FOUND, MISMATCH, OTHER_ACCOUNT,
REPLAY), `authenticity` (supported, unsupported, likely_fake), `same_borrower_loan` (true/false).
Facts for `case_request`: `request` (waiver, settlement), `payment` (none, matched, unconfirmed),
`coverage` (none, full, partial).

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

## STL-01 · Credit bureau reporting of settlements
tags: settlement, credit, bureau, cibil, credit score, report, consequence, what happens, affect
borrower: If a settlement is approved, your loan may be reported to credit bureaus as "settled", which can lower your credit score.
A settled account is reported to credit bureaus as "settled" rather than "closed". This must be disclosed
before a settlement request is submitted.

## WVR-02 · Requests with an unconfirmed payment wait
tags: waiver, settlement, payment, unconfirmed, pending, unverified
decides: case_request
when: payment == unconfirmed
then: action=wait_for_payment
If the payment shared with a waiver or settlement request is not yet confirmed (not in the ledger, or could
not be verified), no request is raised. The borrower is asked to send the request again once the payment is
confirmed.

## WVR-01 · Waiver after a confirmed full payment
tags: waiver, settlement, payment, full, screenshot, matched, not needed
decides: case_request
when: payment == matched and coverage == full
then: action=not_needed
When a waiver or settlement is requested together with a payment that matches the ledger and covers the full
amount due (the overdue amount, or the EMI when nothing is overdue), no request is raised: there is nothing
left to waive or settle.

## PPW-01 · Waiver or settlement after a partial payment
tags: waiver, settlement, payment, partial, shortfall, compliance, escalation
decides: case_request
when: payment == matched and coverage == partial
then: action=confirm_then_submit; tickets=[approval, partial_payment_waiver]; amount_default=shortfall; cite=[APR-01, APR-02, APR-03]
When the payment shared matches the ledger but is less than the amount due, and the borrower asks to waive or
settle the rest, the borrower is shown the shortfall and the applicable clauses. On YES the case enters the
approval stages (APR-01) and compliance review is notified at submission (queue compliance_review), not only
at stage 2. The requested amount defaults to the shortfall unless the borrower states an amount. A partial
payment without such an ask is simply confirmed.

## APR-03 · Borrower confirmation before submission
tags: waiver, settlement, approval, confirm, submit, consent
decides: case_request
when: payment == none
then: action=confirm_then_submit; tickets=[approval]; cite=[APR-01, APR-02]
The agent explains the applicable clauses (APR-01, APR-02, and STL-01 for settlements) and submits the
request only after the borrower replies YES. A NO, or moving on to another topic, cancels the proposal.

## CLM-01 · Payment confirmation needs an exact ledger match
tags: payment, claim, receipt, screenshot, utr, confirm, ledger, lms
decides: payment_claim
when: verdict in [MATCHED, ALREADY_CONFIRMED]
then: action=confirm
A payment is confirmed only when the UTR, amount, date, payee and status match the ledger (D7). A screenshot
or a typed UTR alone never confirms a payment. The receipt PDF is issued on confirmation.

## CLM-05 · Payment on the borrower's other loan
tags: payment, claim, other loan, wrong loan, switch, utr, same borrower
decides: payment_claim
when: verdict == OTHER_ACCOUNT and same_borrower_loan
then: action=suggest_switch
borrower: This payment is linked to your other loan. You can switch to that loan and I'll check the payment there.
When the UTR belongs to another loan on which the same mobile number is an active registered contact, the
borrower picked the wrong loan: it is not treated as a fake and no fraud ticket is raised. The borrower is told
which of their loans it belongs to (masked) and can reply SWITCH LOAN to verify that loan; the receipt is then
checked on it automatically. A UTR on someone else's loan stays under CLM-03.

## CLM-03 · Suspected tampering or fake receipts
tags: payment, claim, fake, tamper, edited, mismatch, other account, replay, fraud, escalation, silent
decides: payment_claim
when: verdict in [MISMATCH, OTHER_ACCOUNT, REPLAY]
when: authenticity == likely_fake
then: action=escalate; ticket=payment_mismatch; notify_borrower=false; cite=[CLM-04, ESC-01]
A receipt that contradicts the ledger (edited amount or date on a real UTR, another loan's UTR, a reused UTR,
wrong payee, future date) or scores as likely fake is escalated to fraud review immediately. The borrower is
NOT told that the receipt was flagged or escalated; they receive a neutral message asking for another receipt
or the UTR. Resending the same receipt does not open a second ticket.

## CLM-02 · Reflection window, then escalation
tags: payment, claim, not reflected, missing, pending, window, escalation, payments team, reconciliation
decides: payment_claim
when: verdict in [PENDING, NOT_FOUND]
then: action=wait_then_escalate; wait_seconds=120; ticket=payment_unmatched; notify_borrower=true; cite=[CLM-01, ESC-01]
borrower: Payments can take a short while to show in our records; if yours doesn't, we'll escalate it to our payments team and give you a reference.
A payment that is not contradicted by the ledger but is not in it yet is rechecked once when the reflection
window ends (demo: 2 minutes from when the borrower sent it). Still missing: the claim is escalated to payment
reconciliation and the borrower is told it couldn't be confirmed, with the ticket reference.

## CTX-01 · Using what the borrower already shared
tags: context, follow-up, already paid, already shared, utr, session, memory
Payment evidence the borrower shared in this chat (UTR, amount, date, payee), voice notes and requests are kept
for 24 hours, even if the verified session expires. On a follow-up without a UTR ("I already paid") the agent
re-checks that evidence against the ledger now, for the loan verified now, instead of asking for it again. It
is used only in verified turns and is never treated as proof by itself.

## CLM-04 · Neutral language and no ledger disclosure
tags: payment, claim, communication, accuse, disclose, neutral
The agent never accuses the borrower and never discloses ledger values (amounts, dates, other accounts) when a
claim does not match.

## HRD-01 · Hardship
tags: hardship, job loss, medical, distress, priority, waiver, settlement
When the borrower mentions hardship, the agent acknowledges it and the request is marked high priority for
the approval queue. Hardship does not change the approval stages.

## HRD-02 · Borrower can't pay in full
tags: hardship, can't pay, unable to pay, partial payment, more time, options, waiver, settlement
borrower: You can pay what you can now, or request a waiver of charges or a settlement; both need approval before anything changes.
A borrower who says they can't pay the full amount is acknowledged and offered the options: a part-payment now
(payment link for the amount they name), a waiver request, or a settlement request. Nothing is raised until they
choose; a waiver or settlement then follows APR-03 (consequences explained, YES before submission). The agent
does not open a settlement on the borrower's behalf, because a settlement can be reported to credit bureaus.

## PRV-01 · Internal information
tags: collector, agency, internal, credit score, bureau data, privacy
Collector or agency assignments, internal notes and bureau data are never shared on WhatsApp. The borrower is
told politely and offered help with their own loan details.

## ESC-02 · The borrower asks to escalate
tags: escalation, escalate, compliance team, manager, supervisor, complaint, discrepancy
borrower: I've passed this to our compliance team; you'll get a reference and they'll update you here.
When the borrower asks to escalate (to compliance, a manager or a person), a compliance_review ticket is raised
straight away, linked to their latest payment claim if there is one, and the borrower gets the reference.
Asking again returns the same reference. If that claim was escalated silently under CLM-03, the reply does not
mention the earlier review.

## ESC-01 · Context carried with every escalation
tags: escalation, ticket, context, approval, compliance, audit
Every ticket and approval case carries a decision record and a context bag: the clause that decided and the
clauses applied, the facts it decided on, the triage (category, sentiment, hardship, priority), the payment
evidence and authenticity score, and the recent conversation (masked). The approval context pack includes the
same clauses so reviewers apply the same policy the agent applied.
