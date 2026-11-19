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

## Open (defaults in effect until decided)

| Decision | Default | Affects |
|---|---|---|
| Can a co-borrower verify on the account? | Yes, with an active `account_contacts` link; sees that account only | T1, T2 |
| Offer already sent, then the borrower changes the request | Cancel in the system, flag `offer_outstanding`; compliance decides how the withdrawal is communicated | T1, T2 |
| Approval order and authority levels | Approver → compliance; authority level from policy band (amount, DPD) | T2 |
| Does a correctly routed complaint count as "resolved without a human"? | No: counted as routed, not auto-resolved | T3 |
