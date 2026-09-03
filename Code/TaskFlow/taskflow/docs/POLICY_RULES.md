# 05 — Policy Rules

The YAML in `config/policies.yaml` is generated from this document, not the other way
around. Every rule has exactly two unit tests: one text that must trigger it and one that
must not.

**Severity semantics**

- `critical` → trips **gate G1** → the response can never be auto-sent, regardless of score
- `warning` → logged, reduces the tone score, never blocks

**Direction** — `outbound` rules inspect the generated draft; `inbound` rules inspect the
customer message and force escalation before drafting.

| # | Rule id | Severity | Direction | What it prevents |
|---|---|---|---|---|
| 1 | `refund_ceiling` | critical | outbound | promising a refund above the $500 agent ceiling |
| 2 | `no_feature_dates` | critical | outbound | committing to a ship date for unreleased work |
| 3 | `no_credential_request` | critical | outbound | asking for a password, 2FA code, or API key |
| 4 | `legal_threat_escalation` | critical | inbound | auto-answering a message that threatens legal action |
| 5 | `gdpr_deletion_routing` | critical | inbound | handling a data-deletion request without the privacy team |
| 6 | `no_discount_invention` | critical | outbound | inventing a discount or free upgrade |
| 7 | `no_security_details` | critical | outbound | disclosing internal infrastructure details |
| 8 | `pricing_accuracy` | warning | outbound | quoting a price that isn't $12 Pro / $29 Enterprise |
| 9 | `no_competitor_disparagement` | warning | outbound | disparaging a third party |
| 10 | `escalation_language_required` | warning | outbound | a complaint or refund reply with no acknowledgement |

## Rule details

**1. `refund_ceiling`** — Support agents may issue refunds up to **$500**. Anything above
requires finance approval and must not be promised. Detector extracts every currency
amount from the draft and compares the maximum against the threshold. Test pair:
`"we'll refund the full $750"` triggers; `"refunds up to $500 are available"` does not.

**2. `no_feature_dates`** — Matches commitment patterns ("will be available in", "should
ship by", "next quarter we will launch"). Test pair: `"SSO will be available in
October"` triggers; `"SSO is on our roadmap, though we can't commit to a date"` does not.

**3. `no_credential_request`** — Matches requests for `password|passcode|2fa|otp|api key`
combined with `send|share|provide|confirm`. Test pair: `"please confirm your password"`
triggers; `"you can reset your password from the sign-in page"` does not.

**4. `legal_threat_escalation`** — Inbound. Matches `lawyer|attorney|legal action|sue|
lawsuit|small claims|gdpr complaint`. Any hit escalates before drafting.

**5. `gdpr_deletion_routing`** — Inbound. Matches deletion requests for account or
personal data. Escalates to the privacy queue; never auto-answered.

**6. `no_discount_invention`** — Matches `NN% off|discount|free month|free upgrade`
in outbound text. Discounts exist only where the knowledge base defines them, and the
model must not create one to resolve a complaint.

**7. `no_security_details`** — Matches disclosure of internal infrastructure ("our
database runs", "internal endpoint", hostnames). Support responses describe behaviour,
not architecture.

**8. `pricing_accuracy`** *(warning)* — Extracts per-seat prices and compares against the
known list ($12 Pro, $29 Enterprise). A mismatch is a strong signal of hallucination even
when grounding passes, so it is worth logging even though it does not block.

**9. `no_competitor_disparagement`** *(warning)* — Matches comparative disparagement.

**10. `escalation_language_required`** *(warning)* — For complaint and refund intents,
requires at least one acknowledgement phrase ("I'm sorry", "I apologise", "I understand
your frustration"). Absence reduces the tone score rather than blocking.

## Adding a rule

1. Add a row to the table above and a details paragraph.
2. Add the rule to `config/policies.yaml` with a detector type the engine supports:
   `amount_over`, `regex_any`, `regex_absent`, `price_mismatch`.
3. Add both unit tests in `tests/unit/test_policy_engine.py`.
4. Run `make scenario S=refund_750` to confirm nothing regressed.

Never add a rule the engine cannot detect deterministically. If a rule genuinely needs
semantic judgement, it belongs in the grounding validator as a score signal, not as a
gate — see ADR-003.
