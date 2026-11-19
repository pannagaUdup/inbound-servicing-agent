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
