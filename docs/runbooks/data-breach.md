# Suspected exposure of personal or health data

This page applies to any sign that data reached someone who should not see it: another
tenant, a log, a public post, a leaked key or a lost device.

1. **Contain (minutes).**
   - Revoke the affected keys (`DELETE /v1/admin/principals/{id}`), or rotate the tenant key
     in the Secret.
   - Disable the affected social account or endpoint.
   - Roll back the release if a deploy caused it.
   - Keep the evidence: do not delete logs or rows.
2. **Scope (hours).** From `audit_events`, list who read what: tenants, subjects, record
   types and time range. The audit is hash-chained, and `agency audit-verify --anchors`
   proves it was not edited.
3. **Notify, against the clock.** The clinic, as data controller, decides and notifies.
   We, as processor, must tell
   each affected clinic **within 2 days** (*término*) of learning of the breach (LOPDP
   Art. 43; processing agreement, clause 7), with the scope from step 2: nature, categories
   and approximate number of patients and records, likely consequences, measures taken.
   Start the clock at the moment we knew, and write that time in the incident log.
   - **Ecuador, LOPDP** (verified in the law's text, `docs/legal/README.md`):
     - the clinic notifies the **SPDP and ARCOTEL** as soon as possible and **at most 5 days**
       (*término*) after it knew, unless the breach is unlikely to put people's rights at risk.
       Late notice must say why (Art. 43).
     - the clinic notifies **each affected patient within 3 days** (*término*) when there is a
       risk to their rights (Art. 46). The exceptions (effective encryption, risk removed) must
       be accepted by the SPDP; disproportionate effort allows a public notice instead.
     - our tools count calendar days, never later than the legal *término*. Whether ARCOTEL
       takes the same form as the SPDP is for the lawyer (`docs/legal/README.md`).
   - **EU GDPR** (only if EU residents are affected): notify the authority within 72 h
     (Art. 33), and the people without undue delay when the risk is high (Art. 34).
   - **HIPAA** (only for US covered entities): notify HHS and the individuals within 60 days.
4. **Fix and prove it.** Add the failing case to `tests/test_tenant_isolation_matrix.py` or
   `tests/test_classification.py` before closing the incident.
