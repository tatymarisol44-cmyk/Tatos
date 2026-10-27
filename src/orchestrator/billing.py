"""Billing domain: clinic appointment revenue and platform subscriptions.

No provider calls, credentials or card details belong in this module. Amounts are
integer cents, and recipients are resolved from operator-owned configuration, never
from a payer's request. A transfer notification is evidence to review, not settlement.
The application must persist these transitions atomically before acknowledging them.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class BillingError(ValueError):
    """An invalid invoice, payment or authorisation transition."""


class ChargeKind(StrEnum):
    APPOINTMENT = "appointment"
    SUBSCRIPTION = "subscription"


class PaymentMethod(StrEnum):
    CARD = "card"
    TRANSFER = "transfer"


class PaymentState(StrEnum):
    PENDING = "pending"
    TRANSFER_REVIEW = "transfer_review"
    PROCESSING = "processing"
    UNCERTAIN = "uncertain"
    PAID = "paid"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


def cents(value: Decimal | str | int) -> int:
    """Convert a dollar amount exactly; refuse floats, fractions of a cent and NaN."""
    if isinstance(value, (bool, float)):
        raise BillingError("use an exact decimal amount")
    try:
        amount = Decimal(value)
        minor = amount * 100
        if not amount.is_finite() or minor != minor.to_integral_value():
            raise BillingError("amount must have at most two decimal places")
        if not 0 < minor <= 99_999_999_999:
            raise BillingError("amount must be positive and within the supported limit")
        return int(minor)
    except (InvalidOperation, TypeError, OverflowError) as exc:
        raise BillingError("invalid amount") from exc


def next_month(anchor: datetime, months: int = 1) -> datetime:
    """Calendar-month anniversary in UTC, calculated from the ORIGINAL anchor.

    January 31 -> February 28 -> March 31; repeatedly adding a month to February
    would silently move the customer's renewal date. No fixed 30-day substitute.
    """
    if anchor.tzinfo is None or months < 0:
        raise BillingError("renewal needs an aware date and nonnegative month count")
    anchor = anchor.astimezone(UTC)
    index = anchor.year * 12 + anchor.month - 1 + months
    year, month_zero = divmod(index, 12)
    month = month_zero + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class Recipient:
    """Verified merchant identity, with a per-merchant binding and no secret value."""

    merchant_id: str
    owner_tenant: str | None  # None: the platform operator, never a clinic merchant

    def __post_init__(self) -> None:
        if not self.merchant_id or self.owner_tenant == "":
            raise BillingError("recipient identity is required")


@dataclass(frozen=True)
class BillingPolicy:
    platform: Recipient
    clinics: tuple[Recipient, ...] = ()
    monthly_price_cents: int | None = None  # unset until the owner sets the price
    currency: str = "USD"

    def __post_init__(self) -> None:
        if self.platform.owner_tenant is not None:
            raise BillingError("platform recipient cannot be a clinic")
        owners = [r.owner_tenant for r in self.clinics]
        ids = [self.platform.merchant_id, *(r.merchant_id for r in self.clinics)]
        if None in owners or len(set(owners)) != len(owners) or len(set(ids)) != len(ids):
            raise BillingError("each recipient must belong to exactly one billing scope")
        if self.currency != "USD":
            raise BillingError("currency is not supported by this policy")
        if self.monthly_price_cents is not None:
            _amount(self.monthly_price_cents)

    def recipient(self, tenant: str, kind: ChargeKind) -> Recipient:
        if kind == ChargeKind.SUBSCRIPTION:
            return self.platform
        if kind != ChargeKind.APPOINTMENT:
            raise BillingError("unknown charge kind")
        for recipient in self.clinics:
            if recipient.owner_tenant == tenant:
                return recipient
        raise BillingError("clinic payment account is not configured")

    def subscription_price(self) -> int:
        if self.monthly_price_cents is None:
            raise BillingError("platform monthly price has not been configured")
        return self.monthly_price_cents


def _amount(value: int) -> None:
    if type(value) is not int or not 0 < value <= 99_999_999_999:
        raise BillingError("amount must be positive integer cents")


@dataclass(frozen=True)
class Invoice:
    tenant: str
    invoice_id: str
    kind: ChargeKind
    resource_id: str  # appointment id, or subscription billing period
    merchant_id: str
    amount_cents: int
    method: PaymentMethod
    currency: str = "USD"
    state: PaymentState = PaymentState.PENDING
    attempt_id: str | None = None
    provider_transaction_id: str | None = None

    def __post_init__(self) -> None:
        _amount(self.amount_cents)
        if not all((self.tenant, self.invoice_id, self.resource_id, self.merchant_id)):
            raise BillingError("invoice identity is required")
        if self.currency != "USD":
            raise BillingError("unsupported invoice currency")

    @classmethod
    def appointment(
        cls,
        policy: BillingPolicy,
        tenant: str,
        invoice_id: str,
        appointment_id: str,
        price: Decimal,
        method: PaymentMethod,
    ) -> Invoice:
        return cls(
            tenant,
            invoice_id,
            ChargeKind.APPOINTMENT,
            appointment_id,
            policy.recipient(tenant, ChargeKind.APPOINTMENT).merchant_id,
            cents(price),
            method,
        )

    @classmethod
    def subscription(
        cls,
        policy: BillingPolicy,
        tenant: str,
        invoice_id: str,
        period: str,
        method: PaymentMethod,
    ) -> Invoice:
        return cls(
            tenant,
            invoice_id,
            ChargeKind.SUBSCRIPTION,
            period,
            policy.recipient(tenant, ChargeKind.SUBSCRIPTION).merchant_id,
            policy.subscription_price(),
            method,
        )

    def notify_transfer(self) -> Invoice:
        if self.method != PaymentMethod.TRANSFER:
            raise BillingError("this invoice is not payable by transfer")
        if self.state == PaymentState.TRANSFER_REVIEW:
            return self
        if self.state != PaymentState.PENDING:
            raise BillingError("invoice is not awaiting a transfer")
        return replace(self, state=PaymentState.TRANSFER_REVIEW)

    def review_transfer(self, *, reviewer_tenant: str | None, approved: bool) -> Invoice:
        # None is an authenticated platform-operator capability, never user input.
        expected = self.tenant if self.kind == ChargeKind.APPOINTMENT else None
        if reviewer_tenant != expected:
            raise BillingError("reviewer does not own this receivable")
        if self.state != PaymentState.TRANSFER_REVIEW:
            raise BillingError("transfer is not awaiting review")
        return replace(self, state=PaymentState.PAID if approved else PaymentState.PENDING)

    def begin_card_attempt(self, attempt_id: str) -> Invoice:
        if self.method != PaymentMethod.CARD or self.state != PaymentState.PENDING:
            raise BillingError("invoice cannot start another card charge")
        if not attempt_id:
            raise BillingError("a durable attempt identity is required before contacting provider")
        return replace(self, state=PaymentState.PROCESSING, attempt_id=attempt_id)

    def uncertain(self) -> Invoice:
        if self.state != PaymentState.PROCESSING:
            raise BillingError("no card attempt is in progress")
        return replace(self, state=PaymentState.UNCERTAIN)

    def settle_card(self, receipt: VerifiedReceipt) -> Invoice:
        """Only the server's authenticated provider adapter may create this receipt.

        A redirect or unsigned webhook cannot itself establish a VerifiedReceipt.
        The storage adapter must also enforce unique merchant/transaction ids.
        """
        if self.method != PaymentMethod.CARD:
            raise BillingError("invoice is not a card payment")
        if (receipt.merchant_id, receipt.attempt_id, receipt.amount_cents, receipt.currency) != (
            self.merchant_id,
            self.attempt_id,
            self.amount_cents,
            self.currency,
        ):
            raise BillingError("provider receipt does not match the invoice")
        if self.state == PaymentState.PAID:
            if receipt.approved and receipt.transaction_id == self.provider_transaction_id:
                return self
            raise BillingError("paid invoice cannot be overwritten")
        if self.state not in (PaymentState.PROCESSING, PaymentState.UNCERTAIN):
            raise BillingError("no card attempt is awaiting settlement")
        return replace(
            self,
            state=PaymentState.PAID if receipt.approved else PaymentState.REJECTED,
            provider_transaction_id=receipt.transaction_id,
        )

    def cancel(self) -> Invoice:
        if self.state not in (PaymentState.PENDING, PaymentState.TRANSFER_REVIEW):
            raise BillingError("in-flight or paid payments need reconciliation or refund")
        return replace(self, state=PaymentState.CANCELLED)


@dataclass(frozen=True)
class VerifiedReceipt:
    merchant_id: str
    attempt_id: str
    transaction_id: str
    amount_cents: int
    currency: str
    approved: bool

    def __post_init__(self) -> None:
        _amount(self.amount_cents)
        if not all((self.merchant_id, self.attempt_id, self.transaction_id)):
            raise BillingError("verified receipt identity is required")


@dataclass(frozen=True)
class RecurringMandate:
    tenant: str
    merchant_id: str
    agreed_price_cents: int
    consent_version: str
    authorised_at: datetime
    cancelled_at: datetime | None = None

    def __post_init__(self) -> None:
        _amount(self.agreed_price_cents)
        if not all((self.tenant, self.merchant_id, self.consent_version)):
            raise BillingError("explicit recurring-payment consent is required")
        if self.authorised_at.tzinfo is None:
            raise BillingError("consent timestamp must be timezone-aware")

    def permits(self, invoice: Invoice) -> bool:
        return (
            self.cancelled_at is None
            and invoice.kind == ChargeKind.SUBSCRIPTION
            and invoice.method == PaymentMethod.CARD
            and invoice.state == PaymentState.PENDING
            and (invoice.tenant, invoice.merchant_id, invoice.amount_cents)
            == (self.tenant, self.merchant_id, self.agreed_price_cents)
        )
