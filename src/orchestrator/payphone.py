"""PayPhone hosted checkout. A browser return is never accepted as proof of payment.

Only fixed PayPhone endpoints receive the merchant token. No card, patient, appointment
or clinical details are sent here. Taxes must be supplied by the merchant's trusted
invoice configuration; this adapter does not infer a service's tax treatment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from orchestrator.billing import BillingError

ORIGIN = "https://pay.payphonetodoesposible.com"
PREPARE = ORIGIN + "/api/button/Prepare"
CONFIRM = ORIGIN + "/api/button/V2/Confirm"


class PayphoneError(BillingError):
    """A bounded error code; provider bodies and credentials never enter the message."""


@dataclass(frozen=True)
class Merchant:
    store_id: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.store_id):
            raise PayphoneError("payphone_configuration_required")
        if not self.token or len(self.token) > 8192 or any(c.isspace() for c in self.token):
            raise PayphoneError("payphone_configuration_required")


@dataclass(frozen=True)
class Amounts:
    total: int
    without_tax: int
    with_tax: int
    tax: int

    def __post_init__(self) -> None:
        values = (self.total, self.without_tax, self.with_tax, self.tax)
        if any(type(value) is not int or not 0 <= value <= 99_999_999_999 for value in values):
            raise PayphoneError("invalid_payment_amount")
        if self.total <= 0 or self.total != self.without_tax + self.with_tax + self.tax:
            raise PayphoneError("invalid_payment_amount")


@dataclass(frozen=True)
class Prepared:
    payment_id: str = field(repr=False)
    card_url: str = field(repr=False)
    payphone_url: str = field(repr=False)


@dataclass(frozen=True)
class Confirmation:
    transaction_id: int
    client_transaction_id: str
    amount_cents: int
    state: str  # paid | cancelled | uncertain; never an unverified client status


def client_reference(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise PayphoneError("invalid_payment_reference")
    return value


def checkout_url(value: Any, payment_id: str, path: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise PayphoneError("invalid_checkout_response")
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.netloc != "pay.payphonetodoesposible.com"
        or url.path != path
        or url.fragment
        or parse_qs(url.query) != {"paymentId": [payment_id]}
    ):
        raise PayphoneError("invalid_checkout_response")
    return value


class Payphone:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def _post(self, url: str, merchant: Merchant, origin: str, body: dict[str, Any]) -> Any:
        # A timeout leaves an uncertain outcome. Calling code must durably reconcile
        # the SAME reference before considering any new payment attempt.
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=20, follow_redirects=False
            ) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": "Bearer " + merchant.token,
                        "Referer": origin + "/",
                    },
                    json=body,
                )
            if response.status_code != 200:
                raise PayphoneError("payment_provider_unavailable")
            value = response.json()
            if not isinstance(value, dict) or "errorCode" in value:
                raise PayphoneError("payment_provider_rejected")
            return value
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, PayphoneError):
                raise
            raise PayphoneError("payment_outcome_uncertain") from None

    @staticmethod
    def _origin(origin: str) -> str:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.netloc != parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise PayphoneError("payment_origin_not_configured")
        return origin

    async def prepare(
        self, merchant: Merchant, origin: str, reference: str, amounts: Amounts
    ) -> Prepared:
        origin = self._origin(origin)
        reference = client_reference(reference)
        result = await self._post(
            PREPARE,
            merchant,
            origin,
            {
                "amount": amounts.total,
                "amountWithoutTax": amounts.without_tax,
                "amountWithTax": amounts.with_tax,
                "tax": amounts.tax,
                "currency": "USD",
                "storeId": merchant.store_id,
                "clientTransactionId": reference,
                "reference": "Pago de servicio",
                "responseUrl": origin + "/v1/billing/payphone/return",
                "cancellationUrl": origin + "/app#ajustes/pagos",
            },
        )
        payment_id = result.get("paymentId")
        if (
            not isinstance(payment_id, str)
            or not 1 <= len(payment_id) <= 256
            or any(ord(char) < 33 for char in payment_id)
        ):
            raise PayphoneError("invalid_checkout_response")
        return Prepared(
            payment_id,
            checkout_url(result.get("payWithCard"), payment_id, "/Anonymous/Index"),
            checkout_url(result.get("payWithPayPhone"), payment_id, "/PayPhone/Index"),
        )

    async def confirm(
        self,
        merchant: Merchant,
        origin: str,
        reference: str,
        transaction_id: int,
        expected_cents: int,
    ) -> Confirmation:
        origin = self._origin(origin)
        reference = client_reference(reference)
        if type(transaction_id) is not int or not 0 < transaction_id <= 9_223_372_036_854_775_807:
            raise PayphoneError("invalid_payment_reference")
        if type(expected_cents) is not int or expected_cents <= 0:
            raise PayphoneError("invalid_payment_amount")
        result = await self._post(
            CONFIRM, merchant, origin, {"id": transaction_id, "clientTxId": reference}
        )
        if (
            result.get("clientTransactionId") != reference
            or type(result.get("transactionId")) is not int
            or result["transactionId"] != transaction_id
            or type(result.get("amount")) is not int
            or result["amount"] != expected_cents
            or result.get("currency") != "USD"
        ):
            raise PayphoneError("payment_confirmation_mismatch")
        code = result.get("statusCode")
        status = result.get("transactionStatus")
        state = (
            "paid"
            if type(code) is int and code == 3 and status == "Approved"
            else "cancelled"
            if type(code) is int and code == 2 and status == "Canceled"
            else "uncertain"
        )
        return Confirmation(transaction_id, reference, expected_cents, state)
