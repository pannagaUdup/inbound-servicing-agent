# Servicing policy: approvals, payment claims and escalations

Version: 2026-11-20 (demo). Owner: Compliance.

Each clause has an ID, tags used for retrieval, an optional **borrower** line (the only wording the agent
may send to a borrower for this clause, approved by compliance) and the internal rule. Borrower lines are
general explanations that are true in any conversation: they are quoted to answer questions about the policy,
so they never say that something has been done or describe the borrower's own situation. The agent retrieves
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
Facts for `waiver_eligibility`: `nudges_given` (times the borrower has already been encouraged to pay, this loan,
last 7 days), `paid_share` (confirmed payments in the last 30 days divided by the current overdue amount),
`dpd_band` (0-10, 10-20, ...), `reason_known` (a recognised hardship reason was given).
Facts for `settlement_preapproval`: `preapproved_flag` (accounts.preapproved_settlement = Y), `recovery` (share of
the principal recovered), `dpd_band`.
Facts for `hardship`: `nudges_given`, `stance` (unable_now, pay_part, ask_waiver, ask_settlement, ask_instalments,
refuse_all), `waiver_eligible`, `waiver_offered`, `waiver_declines`, `preapproved`, `settlement_offered` (the first
two from the situations above).

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

## APR-04 · Request details agreed before confirmation
tags: waiver, settlement, amount, reason, details, confirm, submit
Before a waiver or settlement is put to the borrower for YES / NO, its amount and reason are agreed with them:
for a settlement, the amount they offer to pay; for a waiver, the amount or the charges to waive; and the reason
(job loss, medical expenses, business loss, salary delay, reduced income, family emergency, financial difficulty,
or their own words). Missing details are asked for in one message; after two unanswered asks nothing is raised.
A question about waivers or settlements ("why does it need approval?") is answered, not treated as a request.

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
borrower: If a payment belongs to another of your loans, you can switch to that loan and we'll check the payment there.
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
Each verified session has its own context window (voice notes, receipts, requests and decisions in that
session); it is not carried into a new session. Payment evidence is different: every claim is recorded against
the loan, so on a follow-up without a UTR ("I already paid"), even in a new session, the agent takes the latest
claim on the loan verified now (last 24 hours) and, if it is still unresolved, re-checks it against the ledger
instead of asking for the UTR again. It is used only in verified turns and is never treated as proof by itself.

## CLM-04 · Neutral language and no ledger disclosure
tags: payment, claim, communication, accuse, disclose, neutral
The agent never accuses the borrower and never discloses ledger values (amounts, dates, other accounts) when a
claim does not match.

## WVR-03 · Waiver eligibility: 10-30 days past due
tags: waiver, eligibility, charges, days past due
decides: waiver_eligibility
when: dpd_band in [10-20, 20-30] and nudges_given >= 3
then: action=eligible
borrower: If your loan is 10-30 days past due, you can request a waiver of the charges; once it's approved you pay the rest the same day.
A waiver of the late-payment and bounce charges is available only for a loan 10-30 days past due, after three rounds
of encouragement, and only when there are charges to waive. Loans under 10 or over 30 days past due are not eligible.

## STL-05 · Pre-approved settlement: flagged accounts
tags: settlement, pre-approved, preapproved, one-time, eligibility, collections
decides: settlement_preapproval
when: preapproved_flag
then: action=preapproved; amount_pct=0.6
Accounts flagged by the collections team (accounts.preapproved_settlement = Y) may be offered a pre-approved,
one-time settlement of amount_pct of the total outstanding (principal outstanding plus overdue amount).

## STL-06 · Pre-approved settlement: high recovery
tags: settlement, pre-approved, recovery, bucket, eligibility
decides: settlement_preapproval
when: recovery >= 0.9 and dpd_band in [0-10]
then: action=preapproved; amount_pct=0.6
Loans where at least 90% of the principal has been recovered are pre-approved for a one-time settlement when they
fall in the configured delinquency bucket (demo: 0-10 days past due; in production typically bucket 3). The bucket
list and the recovery threshold are edited here.

## STL-07 · Settlement in instalments goes to collections
tags: settlement, instalments, tranches, parts, multiple payments, collections, ops
decides: hardship
when: settlement_offered and stance == ask_instalments
then: action=route_collections
A pre-approved settlement is one payment. A borrower who wants to pay it in instalments (multiple tranches) is
forwarded to the collections team (ops queue) and told the team will reach out.

## STL-03 · Pre-approved settlement as the last resort
tags: settlement, last resort, pre-approved, one-time, payment link, waiver declined
decides: hardship
when: nudges_given >= 3 and preapproved and waiver_declines >= 2
when: nudges_given >= 3 and preapproved and not waiver_eligible
then: action=offer_preapproved_settlement; cite=[STL-01]
borrower: If your loan qualifies for a pre-approved settlement, you can close the dues with one payment; it may be reported to credit bureaus as "settled".
Only after three rounds of encouragement, and only when the waiver was declined twice or isn't available, a loan with
a pre-approved settlement (STL-05/06) is offered it: the one-time amount, the credit-bureau consequence (STL-01) and
the payment link straight away. No approval is needed; the collections queue is notified of the offer.

## STL-04 · No pre-approved settlement: collections team
tags: settlement, collections, ops, not pre-approved, last resort
decides: hardship
when: nudges_given >= 3 and waiver_declines >= 2
when: nudges_given >= 3 and not waiver_eligible
then: action=route_collections
borrower: When we can't find an option here, our collections team looks at your case and reaches out to you.
Without a pre-approved settlement, the agent does not offer or negotiate one: after three rounds of encouragement (and
the waiver declined twice or not available) the case is forwarded to the collections team and the borrower is told.

## WVR-05 · Waiver of charges: approval first, then a same-day payment
tags: waiver, offer, charges, approval, same day, payment link, deadline, overdue
decides: hardship
when: waiver_eligible
then: action=offer_waiver; cite=[APR-01, APR-02]
When WVR-03 says the borrower is eligible, the agent tells them: the charges can be taken off the overdue amount
after a supervisor approves it; once approved they get a payment link for the overdue amount minus the charges, which
must be paid the same day, otherwise the waiver is withdrawn and the charges apply again from the next day. The
borrower answers YES or NO.
- YES: the waiver of the charges goes for approval (APR-01: supervisor, then compliance; it may be approved in part or
  rejected). The borrower gets the reference.
- On approval: a payment link for the overdue minus the charges, valid until 23:59 IST that day.
- Paid that day: the overdue becomes zero and the charges are waived; the borrower gets the receipt.
- Not paid that day: the link expires, the waiver is withdrawn, the overdue (with the charges) stays, and the borrower
  is told.
- NO, or still saying they can't pay, is a decline: the offer is made once more, and after two declines the ladder
  moves on (STL-03 / STL-04).

## HRD-03 · Encourage payment first
tags: hardship, can't pay, encourage, due date, credit score, consequence, payment
decides: hardship
when: nudges_given >= 0
then: action=encourage_payment
borrower: Paying on time keeps your credit score healthy and stops late charges from adding up; even a part payment helps.
Waivers and settlements are a last resort and are not offered up front. A borrower who says they can't pay is first
acknowledged with empathy and told, factually, what is due and when, what late or missed payment means for their
credit score, and that a part payment now helps (payment link on request). An explicit request for a waiver or
settlement before the borrower qualifies is answered the same way. This clause matches when no clause above does.

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
borrower: You can ask us to escalate a problem to our compliance team at any time. We'll confirm with you first, then give you a reference, and they'll update you here.
When the borrower asks to escalate (to compliance, a manager or a person), the agent asks them to confirm
(YES / NO) first. On YES a compliance_review ticket is raised, linked to their latest payment claim if there is
one, and the borrower gets the reference. NO, or moving on, raises nothing.
Asking again returns the same reference. If that claim was escalated silently under CLM-03, the reply does not
mention the earlier review.

## DUP-01 · The same receipt sent again
tags: payment, claim, receipt, resend, duplicate, again, same, limit, retry, escalation
then: max_resends=3
borrower: There's no need to send the same receipt more than once: we check it once and update you here.
A receipt (same UTR, same loan) that was already received is not processed as new: no second ticket and no
second check. While its check is running the borrower is told how long is left; once it has been escalated
they get the existing reference (or, after a silent review under CLM-03, the same neutral reply). After
max_resends sends of the same receipt the agent stops re-checking it (a hard stop): the attempt is logged and
the borrower is told it's already with the team. A receipt that now matches the ledger is still confirmed.

## DUP-02 · A request already in approval
tags: waiver, settlement, approval, duplicate, again, change, update, restart, reinitiate, limit, in progress
then: max_revisions=2
borrower: If you change a request that's already in approval, the earlier request is cancelled and the approval restarts from the first step.
When the loan already has a waiver or settlement request in approval, the borrower is told its reference and
stage instead of a new request being raised. Asking for the same thing again changes nothing. Asking for
something different (amount, type, components, plan) is confirmed first: the agent explains that the current
request will be cancelled and the approval restarted from the first stage (APR-01), and changes it only on YES.
A request can be changed at most max_revisions times (a hard stop); after that it stays as it is and the
borrower is pointed to the helpline.

## SEC-01 · Recently added mobile number (SIM-swap check)
tags: security, sim swap, new number, recently added, identity, documents, statement, waiver, settlement
then: recent_days=7
borrower: For your security, if your mobile number was added to your loan recently, our team verifies it before documents are sent.
If the verified mobile number was added to the loan within recent_days, documents (statement, receipt, closure
quote) are not sent automatically: a sim_swap_review ticket goes to the security team, which verifies the
borrower and sends them. A waiver or settlement request is still raised after the borrower's YES, but it
carries the flag and a linked security ticket, and the approver sees both. Answers about the loan itself are
unaffected. One open review covers later requests; asking again returns the same reference.

## ESC-01 · Context carried with every escalation
tags: escalation, ticket, context, approval, compliance, audit
Every ticket and approval case carries a decision record and a context bag: the clause that decided and the
clauses applied, the facts it decided on, the triage (category, sentiment, hardship, priority), the payment
evidence and authenticity score, and the recent conversation (masked). The approval context pack includes the
same clauses so reviewers apply the same policy the agent applied.
