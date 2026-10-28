"""PayPhone's documented contract with a fake transport, not live financial evidence."""

import json

import httpx
import pytest

from orchestrator.payphone import Amounts, Merchant, Payphone, PayphoneError

REFERENCE = "a" * 32
MERCHANT = Merchant("synthetic-store", "synthetic-merchant-credential")
APP = "https://tatos.example"


async def test_prepare_uses_fixed_host_trusted_amount_and_no_clinical_details() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://pay.payphonetodoesposible.com/api/button/Prepare"
        assert request.headers["authorization"] == "Bearer synthetic-merchant-credential"
        assert request.headers["referer"] == APP + "/"
        body = json.loads(request.content)
        assert body == {
            "amount": 10000,
            "amountWithoutTax": 0,
            "amountWithTax": 9000,
            "tax": 1000,
            "currency": "USD",
            "storeId": "synthetic-store",
            "clientTransactionId": REFERENCE,
            "reference": "Pago de servicio",
            "responseUrl": APP + "/v1/billing/payphone/return",
            "cancellationUrl": APP + "/app#ajustes/pagos",
        }
        return httpx.Response(
            200,
            json={
                "paymentId": "synthetic-payment",
                "payWithCard": "https://pay.payphonetodoesposible.com/Anonymous/Index?paymentId=synthetic-payment",
                "payWithPayPhone": "https://pay.payphonetodoesposible.com/PayPhone/Index?paymentId=synthetic-payment",
            },
        )

    prepared = await Payphone(httpx.MockTransport(handle)).prepare(
        MERCHANT, APP, REFERENCE, Amounts(10000, 0, 9000, 1000)
    )
    assert prepared.payment_id == "synthetic-payment"
    assert "synthetic-payment" not in repr(prepared)
    assert MERCHANT.token not in repr(MERCHANT)


@pytest.mark.parametrize(
    "override",
    [
        {"clientTransactionId": "b" * 32},
        {"transactionId": 456},
        {"amount": 9999},
        {"currency": "EUR"},
        {"amount": "10000"},
        {"transactionId": "123"},
    ],
)
async def test_other_invoices_amounts_and_currencies_cannot_confirm_a_payment(
    override: dict,
) -> None:
    body = {
        "clientTransactionId": REFERENCE,
        "transactionId": 123,
        "amount": 10000,
        "currency": "USD",
        "statusCode": 3,
        "transactionStatus": "Approved",
        **override,
    }
    gateway = Payphone(httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    with pytest.raises(PayphoneError, match="payment_confirmation_mismatch"):
        await gateway.confirm(MERCHANT, APP, REFERENCE, 123, 10000)


@pytest.mark.parametrize(
    ("code", "status", "expected"),
    [
        (3, "Approved", "paid"),
        (2, "Canceled", "cancelled"),
        (1, "Pending", "uncertain"),
        (3, "Canceled", "uncertain"),
        ("3", "Approved", "uncertain"),
    ],
)
async def test_confirm_uses_authenticated_provider_result(code, status, expected) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/button/V2/Confirm"
        assert json.loads(request.content) == {"id": 123, "clientTxId": REFERENCE}
        return httpx.Response(
            200,
            json={
                "clientTransactionId": REFERENCE,
                "transactionId": 123,
                "amount": 10000,
                "currency": "USD",
                "statusCode": code,
                "transactionStatus": status,
                "email": "synthetic@example.test",
                "document": "not-real",
                "cardToken": "not-real",
            },
        )

    result = await Payphone(httpx.MockTransport(handle)).confirm(
        MERCHANT, APP, REFERENCE, 123, 10000
    )
    assert result.state == expected
    assert "example.test" not in repr(result) and "not-real" not in repr(result)


@pytest.mark.parametrize(
    "url",
    [
        "http://pay.payphonetodoesposible.com/Anonymous/Index?paymentId=synthetic",
        "https://pay.payphonetodoesposible.com.evil.test/Anonymous/Index?paymentId=synthetic",
        "https://pay.payphonetodoesposible.com/Anonymous/Index?paymentId=someone-else",
        "https://attacker:credential@pay.payphonetodoesposible.com/Anonymous/Index?paymentId=synthetic",
    ],
)
async def test_provider_cannot_return_an_arbitrary_redirect(url: str) -> None:
    gateway = Payphone(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "paymentId": "synthetic",
                    "payWithCard": url,
                    "payWithPayPhone": "https://pay.payphonetodoesposible.com/PayPhone/Index?paymentId=synthetic",
                },
            )
        )
    )
    with pytest.raises(PayphoneError, match="invalid_checkout_response"):
        await gateway.prepare(MERCHANT, APP, REFERENCE, Amounts(10000, 10000, 0, 0))


async def test_timeout_and_redirect_are_uncertain_without_automatic_retry() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic credential must not appear in error", request=request)

    gateway = Payphone(httpx.MockTransport(handle))
    with pytest.raises(PayphoneError, match=r"^payment_outcome_uncertain$"):
        await gateway.prepare(MERCHANT, APP, REFERENCE, Amounts(10000, 10000, 0, 0))
    assert calls == 1
    gateway = Payphone(
        httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "https://evil.test"})
        )
    )
    with pytest.raises(PayphoneError, match=r"^payment_provider_unavailable$"):
        await gateway.prepare(MERCHANT, APP, REFERENCE, Amounts(10000, 10000, 0, 0))


@pytest.mark.parametrize("values", [(10000, 9000, 0, 0), (True, 1, 0, 0), (10000, -1, 10001, 0)])
def test_amounts_require_an_exact_explicit_breakdown(values) -> None:
    with pytest.raises(PayphoneError, match="invalid_payment_amount"):
        Amounts(*values)
