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
3. **Decide whether to notify.** The clinic, as data controller, decides. We, as processor,
   tell the clinic **without undue delay** and give it the scope from step 2.
   - **Ecuador, LOPDP:** notice goes to the authority and to the people affected. The exact
     deadline and form are **TO VERIFY with the lawyer**; the question is tracked in
     `docs/private/`.
   - **EU GDPR** (only if EU residents are affected): notify the authority within 72 h
     (Art. 33), and the people without undue delay when the risk is high (Art. 34).
   - **HIPAA** (only for US covered entities): notify HHS and the individuals within 60 days.
4. **Fix and prove it.** Add the failing case to `tests/test_tenant_isolation_matrix.py` or
   `tests/test_classification.py` before closing the incident.
