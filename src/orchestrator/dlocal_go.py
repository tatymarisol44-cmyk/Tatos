"""dLocal Go hosted checkout contract (not the enterprise dLocal API).

Only opaque order references leave Tatos. No card, clinical or patient profile
fields are sent or retained. Provider responses are never logged wholesale.
Official contracts: docs.dlocalgo.com/integration-api/welcome-to-dlocal-go-api.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from orchestrator.billing import BillingError

Environment = Literal["sandbox", "live"]
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class GatewayError(BillingError):
    """An unsuccessful or uncertain provider operation, without response secrets."""


def identifier(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise GatewayError("invalid provider identifier")
    result = str(value)
    if not _ID.fullmatch(result):
        raise GatewayError("invalid provider identifier")
    return result


def amount_cents(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise GatewayError("invalid provider amount")
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise GatewayError("invalid provider amount")
        result = int(amount)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise GatewayError("invalid provider amount") from exc
    if not 0 < result <= 99_999_999_999:
        raise GatewayError("invalid provider amount")
    return result


@dataclass(frozen=True)
class Credentials:
    environment: Environment
    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.environment not in ("sandbox", "live") or any(
            not re.fullmatch(r"[A-Za-z0-9_./+=-]{8,512}", key)
            for key in (self.api_key, self.secret_key)
        ):
            raise GatewayError("invalid dLocal Go credentials")

    @property
    def host(self) -> str:
        return "api-sbx.dlocalgo.com" if self.environment == "sandbox" else "api.dlocalgo.com"

    def checkout_url(self, value: Any, *, subscription: bool = False) -> str:
        if not isinstance(value, str) or len(value) > 2048:
            raise GatewayError("invalid checkout destination")
        url = urlsplit(value)
        host = (
            "checkout-sbx.dlocalgo.com"
            if self.environment == "sandbox"
            else "checkout.dlocalgo.com"
        )
        path = (
            r"/validate/subscription/[A-Za-z0-9_-]+"
            if subscription
            else r"/validate/[A-Za-z0-9_-]+"
        )
        if (
            url.scheme != "https"
            or url.netloc != host
            or not re.fullmatch(path, url.path)
            or url.query
            or url.fragment
        ):
            raise GatewayError("invalid checkout destination")
        return value

    def valid_signature(self, payload: bytes, authorization: str) -> bool:
        match = re.fullmatch(r"V2-HMAC-SHA256, Signature: ([a-fA-F0-9]{64})", authorization)
        if match is None:
            return False
        expected = hmac.new(
            self.secret_key.encode(), self.api_key.encode() + payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, match[1].lower())


def validate_payment(
    data: dict[str, Any], *, reference: str, cents: int, payment_id: str | None = None
) -> str:
    identifier(data.get("id"))
    if (
        data.get("order_id") != reference
        or data.get("currency") != "USD"
        or data.get("country") != "EC"
        or amount_cents(data.get("amount")) != cents
        or (payment_id is not None and data["id"] != payment_id)
    ):
        raise GatewayError("provider payment does not match the order")
    status = data.get("status")
    states = {
        "PENDING": "processing",
        "PAID": "paid",
        "REJECTED": "rejected",
        "CANCELLED": "cancelled",
        "EXPIRED": "cancelled",
    }
    if not isinstance(status, str) or status not in states:
        raise GatewayError("unknown provider payment state")
    return states[status]


def validate_plan(data: dict[str, Any], cents: int, plan_id: str | None = None) -> None:
    identifier(data.get("id"))
    if (
        (plan_id is not None and str(data["id"]) != plan_id)
        or data.get("currency") != "USD"
        or data.get("country") != "EC"
        or amount_cents(data.get("amount")) != cents
        or data.get("frequency_type") != "MONTHLY"
        or type(data.get("frequency_value")) is not int
        or data["frequency_value"] != 1
        or data.get("free_trial_days", 0) != 0
    ):
        raise GatewayError("provider plan does not match the monthly subscription")


class DlocalGo:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def request(
        self,
        credentials: Credentials,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        page: int | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=20, follow_redirects=False
            ) as client:
                response = await client.request(
                    method,
                    f"https://{credentials.host}/v1/{path}",
                    headers={
                        "Authorization": f"Bearer {credentials.api_key}:{credentials.secret_key}",
                        "Content-Type": "application/json",
                    },
                    content=json.dumps(body, separators=(",", ":")) if body is not None else None,
                    params={"page": page, "page_size": 100} if page is not None else None,
                )
        except httpx.HTTPError:
            raise GatewayError("dLocal Go result is uncertain; reconcile before retrying") from None
        if response.status_code not in (200, 201):
            raise GatewayError("dLocal Go operation was not confirmed")
        try:
            data = json.loads(response.content, parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise GatewayError("invalid dLocal Go response") from None
        if not isinstance(data, dict):
            raise GatewayError("invalid dLocal Go response")
        return data

    async def account(self, credentials: Credentials) -> None:
        if not await self.request(credentials, "GET", "me"):
            raise GatewayError("dLocal Go account was not verified")

    async def create_payment(
        self,
        credentials: Credentials,
        reference: str,
        cents: int,
        origin: str,
    ) -> dict[str, Any]:
        identifier(reference)
        amount_cents(Decimal(cents) / 100)
        return await self.request(
            credentials,
            "POST",
            "payments",
            {
                "order_id": reference,
                "country": "EC",
                "currency": "USD",
                "amount": cents / 100,
                "description": "Pago de servicio",
                "payment_type": "CREDIT_CARD,DEBIT_CARD",
                "notification_url": f"{origin}/v1/billing/dlocal/hooks/{reference}",
                "success_url": f"{origin}/v1/billing/dlocal/return",
                "back_url": f"{origin}/v1/billing/dlocal/return",
            },
        )

    async def payment(self, credentials: Credentials, payment_id: str) -> dict[str, Any]:
        return await self.request(credentials, "GET", f"payments/{identifier(payment_id)}")

    async def create_plan(
        self,
        credentials: Credentials,
        reference: str,
        cents: int,
        origin: str,
    ) -> dict[str, Any]:
        identifier(reference)
        amount_cents(Decimal(cents) / 100)
        return await self.request(
            credentials,
            "POST",
            "subscription/plan",
            {
                "name": f"Tatos {reference}",
                "description": "Uso mensual de Tatos por consultorio",
                "country": "EC",
                "currency": "USD",
                "amount": cents / 100,
                "frequency_type": "MONTHLY",
                "frequency_value": 1,
                "notification_url": f"{origin}/v1/billing/dlocal/hooks/{reference}",
                "success_url": f"{origin}/app#ajustes/pagos",
                "back_url": f"{origin}/app#ajustes/pagos",
                "error_url": f"{origin}/app#ajustes/pagos",
            },
        )

    async def plan(self, credentials: Credentials, plan_id: str) -> dict[str, Any]:
        return await self.request(credentials, "GET", f"subscription/plan/{identifier(plan_id)}")

    async def pages(self, credentials: Credentials, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        # Never silently use an incomplete first page to decide that no mandate exists.
        for page in range(1, 21):
            result = await self.request(credentials, "GET", path, page=page)
            data, total = result.get("data"), result.get("total_pages")
            if (
                not isinstance(data, list)
                or any(not isinstance(r, dict) for r in data)
                or type(total) is not int
                or total < 0
            ):
                raise GatewayError("invalid dLocal Go page")
            items.extend(data)
            if page >= total:
                return items
        raise GatewayError("dLocal Go reconciliation needs additional pages")

    async def subscribers(self, credentials: Credentials, plan_id: str) -> list[dict[str, Any]]:
        return await self.pages(
            credentials, f"subscription/plan/{identifier(plan_id)}/subscription/all"
        )

    async def executions(
        self,
        credentials: Credentials,
        plan_id: str,
        subscription_id: str,
    ) -> list[dict[str, Any]]:
        return await self.pages(
            credentials,
            f"subscription/plan/{identifier(plan_id)}/subscription/"
            f"{identifier(subscription_id)}/execution/all",
        )

    async def cancel_plan(self, credentials: Credentials, plan_id: str) -> None:
        data = await self.request(
            credentials, "PATCH", f"subscription/plan/{identifier(plan_id)}/deactivate"
        )
        if str(data.get("id")) != plan_id or data.get("active") is not False:
            raise GatewayError("monthly plan cancellation was not confirmed")
