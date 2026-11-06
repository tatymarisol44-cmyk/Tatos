"""Financial invariants with synthetic merchants; no gateway or real money."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from orchestrator.billing import (
    BillingError,
    BillingPolicy,
    ChargeKind,
    Invoice,
    PaymentMethod,
    PaymentState,
    Recipient,
    RecurringMandate,
    VerifiedReceipt,
    cents,
    next_month,
)


@pytest.fixture
def policy() -> BillingPolicy:
    return BillingPolicy(
        Recipient("platform-store", None),
        (Recipient("clinic-a-store", "acme"), Recipient("clinic-b-store", "globex")),
        monthly_price_cents=2500,
    )


def appointment(policy: BillingPolicy, method: PaymentMethod = PaymentMethod.CARD) -> Invoice:
    return Invoice.appointment(
        policy, "acme", "invoice-demo", "visit-demo", Decimal("49.99"), method
    )


def subscription(policy: BillingPolicy, method: PaymentMethod = PaymentMethod.CARD) -> Invoice:
    return Invoice.subscription(policy, "acme", "subscription-demo", "2026-11", method)


def receipt(invoice: Invoice, **changes: object) -> VerifiedReceipt:
    result = VerifiedReceipt(
        invoice.merchant_id,
        invoice.attempt_id or "attempt-demo",
        "transaction-demo",
        invoice.amount_cents,
        invoice.currency,
        True,
    )
    return replace(result, **changes)  # type: ignore[arg-type]


def test_clinic_revenue_and_platform_revenue_have_distinct_recipients(
    policy: BillingPolicy,
) -> None:
    own = appointment(policy)
    app = subscription(policy)
    assert own.merchant_id == "clinic-a-store" and own.amount_cents == 4999
    assert app.merchant_id == "platform-store" and app.amount_cents == 2500
    other = Invoice.appointment(
        policy, "globex", "other-invoice", "other-visit", Decimal("15"), PaymentMethod.CARD
    )
    assert other.merchant_id == "clinic-b-store"
    with pytest.raises(BillingError, match="not configured"):
        policy.recipient("unconfigured-clinic", ChargeKind.APPOINTMENT)


@pytest.mark.parametrize("clinic", [Recipient("platform-store", "acme"), Recipient("x", None)])
def test_merchant_identity_cannot_be_shared_between_billing_scopes(clinic: Recipient) -> None:
    with pytest.raises(BillingError):
        BillingPolicy(Recipient("platform-store", None), (clinic,))


def test_subscription_has_no_invented_price(policy: BillingPolicy) -> None:
    unset = replace(policy, monthly_price_cents=None)
    with pytest.raises(BillingError, match="not been configured"):
        subscription(unset)
    with pytest.raises(BillingError):
        replace(policy, monthly_price_cents=0)


@pytest.mark.parametrize(
    "value,expected", [("0.01", 1), ("49.99", 4999), (Decimal("25.00"), 2500), (1, 100)]
)
def test_exact_amounts(value: Decimal | str | int, expected: int) -> None:
    assert cents(value) == expected


@pytest.mark.parametrize(
    "value", ["NaN", "sNaN", "Infinity", "-Infinity", "0", "-1", "1.001", "bad", "1000000000"]
)
def test_invalid_money_is_refused(value: str) -> None:
    with pytest.raises(BillingError):
        cents(value)


@pytest.mark.parametrize("value", [True, 49.99])
def test_float_and_boolean_amounts_are_not_silently_rounded(value: object) -> None:
    with pytest.raises(BillingError):
        cents(value)  # type: ignore[arg-type]


def test_transfer_notification_never_establishes_received_funds(policy: BillingPolicy) -> None:
    invoice = appointment(policy, PaymentMethod.TRANSFER)
    pending = invoice.notify_transfer()
    assert pending.state == PaymentState.TRANSFER_REVIEW
    assert pending.notify_transfer() == pending
    assert pending.review_transfer(reviewer_tenant="acme", approved=True).state == PaymentState.PAID
    assert (
        pending.review_transfer(reviewer_tenant="acme", approved=False).state
        == PaymentState.PENDING
    )
    for foreign in ["globex", None]:
        with pytest.raises(BillingError):
            pending.review_transfer(reviewer_tenant=foreign, approved=True)


def test_clinic_cannot_approve_its_own_platform_subscription(policy: BillingPolicy) -> None:
    pending = subscription(policy, PaymentMethod.TRANSFER).notify_transfer()
    for tenant in ["acme", "globex"]:
        with pytest.raises(BillingError):
            pending.review_transfer(reviewer_tenant=tenant, approved=True)
    assert pending.review_transfer(reviewer_tenant=None, approved=True).state == PaymentState.PAID


def test_receipt_belongs_to_the_exact_merchant_attempt_amount_and_currency(
    policy: BillingPolicy,
) -> None:
    invoice = appointment(policy).begin_card_attempt("attempt-demo")
    for changes in [
        dict(merchant_id="platform-store"),
        dict(merchant_id="clinic-b-store"),
        dict(attempt_id="another-attempt"),
        dict(amount_cents=1),
        dict(currency="EUR"),
    ]:
        with pytest.raises(BillingError):
            invoice.settle_card(receipt(invoice, **changes))
    assert invoice.settle_card(receipt(invoice)).state == PaymentState.PAID


def test_timeout_requires_reconciliation_instead_of_another_charge(policy: BillingPolicy) -> None:
    invoice = appointment(policy).begin_card_attempt("attempt-demo").uncertain()
    assert invoice.state == PaymentState.UNCERTAIN
    with pytest.raises(BillingError):
        invoice.begin_card_attempt("retry-that-might-double-charge")
    with pytest.raises(BillingError):
        invoice.cancel()
    assert invoice.settle_card(receipt(invoice)).state == PaymentState.PAID


def test_duplicate_provider_confirmation_does_not_repeat_settlement(policy: BillingPolicy) -> None:
    invoice = appointment(policy).begin_card_attempt("attempt-demo")
    confirmed = receipt(invoice)
    paid = invoice.settle_card(confirmed)
    assert paid.settle_card(confirmed) == paid
    with pytest.raises(BillingError):
        paid.settle_card(replace(confirmed, transaction_id="another-payment"))
    with pytest.raises(BillingError):
        paid.settle_card(replace(confirmed, approved=False))


def test_cancelled_or_paid_invoices_cannot_start_a_charge(policy: BillingPolicy) -> None:
    invoice = appointment(policy)
    for closed in [
        invoice.cancel(),
        invoice.begin_card_attempt("attempt-demo").settle_card(receipt(invoice)),
    ]:
        with pytest.raises(BillingError):
            closed.begin_card_attempt("another-attempt")


def test_card_receipt_cannot_settle_a_bank_transfer(policy: BillingPolicy) -> None:
    invoice = appointment(policy, PaymentMethod.TRANSFER).notify_transfer()
    with pytest.raises(BillingError):
        invoice.settle_card(receipt(invoice))


def test_monthly_renewal_uses_the_original_anniversary_not_thirty_days() -> None:
    anchor = datetime(2027, 1, 31, 12, tzinfo=UTC)
    assert next_month(anchor, 1) == datetime(2027, 2, 28, 12, tzinfo=UTC)
    assert next_month(anchor, 2) == datetime(2027, 3, 31, 12, tzinfo=UTC)
    assert next_month(anchor, 12) == datetime(2028, 1, 31, 12, tzinfo=UTC)
    assert next_month(datetime(2028, 1, 31, tzinfo=UTC), 1).day == 29


def test_recurring_consent_is_for_one_tenant_merchant_and_agreed_price(
    policy: BillingPolicy,
) -> None:
    invoice = subscription(policy)
    consent = RecurringMandate("acme", "platform-store", 2500, "terms-v1", datetime.now(UTC))
    assert consent.permits(invoice)
    for changed in [
        replace(invoice, tenant="globex"),
        replace(invoice, merchant_id="clinic-a-store"),
        replace(invoice, amount_cents=3500),
        replace(invoice, method=PaymentMethod.TRANSFER),
        replace(invoice, state=PaymentState.PAID),
    ]:
        assert not consent.permits(changed)
    assert not replace(consent, cancelled_at=datetime.now(UTC)).permits(invoice)
    assert not consent.permits(appointment(policy))
