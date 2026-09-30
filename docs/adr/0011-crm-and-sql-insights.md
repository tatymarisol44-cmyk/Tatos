# ADR 0011: CRM data layer and SQL-computed insights

**Status:** accepted

## Context

The orchestrator is growing into an AI-native CRM/ERP for small businesses, first for dental clinics. The e-CRM literature (Angeloska-Dichovska and Angeleski) distinguishes operational CRM (contacts, appointments, orders), analytical CRM (segmentation, BI) and collaborative CRM (channels). Studies of AI adoption in SMEs (Saraguro et al., 2025) find that data must be organised before AI helps, and that owners need insights without technical skills. Pearson et al. (arXiv:2607.19297) propose a text-to-SQL recipe with validation and repair loops for analytics.

## Options for insights

1. **Free text-to-SQL** (generate, validate, execute, repair). Flexible, but with health data a generated query could read columns it should not, and a wrong number stated confidently is worse than a missing chart.
2. **Predefined, tested SQL metrics, narrated by the LLM.** *Chosen.* The LLM sees an aggregate JSON keyed by pseudonymous ids, never names or contact data, and is told to name the metric behind every claim.

## Decision

- `crm.py`: patients (the id is the subject id everywhere), appointments with a status machine (`scheduled → confirmed → completed | no_show | cancelled`), and treatment plans with the pack's pipeline stages. Every write, and every human read of a patient record, is an audit event.
- **Traffic-light alerts** (after the Starwood e-CRM case): unconfirmed appointments inside 48 h/24 h, quotes unanswered after N/2N days, and recalls due or 60 days overdue (unless the next visit is already booked). Thresholds come from the pack.
- **Erasure with clinical retention:** contact data is removed and the record becomes `restricted`, while appointments and treatments are retained as the legal clinical record (GDPR Art. 17(3)). Restricted records drop out of alerts, insights and campaigns and refuse new writes.
- `insights.py`: rule-based RFM segments (champion, loyal, new, at_risk, dormant, no_visits, occasional), with the pack's recall interval as the time unit; top-20% monetary value; smoothed no-show risk for upcoming appointments; pipeline value by stage; and a naive 8-week forecast labelled as such. `POST /v1/insights/ask` runs the input guard, then narrates the metrics.
- One SQL code path for SQLite (tests) and Postgres (prod). A CI-only test (`test_db_postgres.py`) runs the whole flow on real Postgres to catch dialect differences.

## Consequences

- Owners ask questions in natural language, and every number is reproducible from SQL.
- New questions need new metrics (code plus tests). A guarded text-to-SQL recipe (read-only role, allow-listed views, repair loop) can be added later for exploratory analysis.
- No ERP ledger yet: revenue comes from visit prices and accepted treatment plans. Invoicing, inventory with batches and expiry dates, and payments are the next module.
