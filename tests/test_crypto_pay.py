import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from premium_bot.api import create_app
from premium_bot.application import payments
from premium_bot.db import Invoice, Setting, Wallet
from premium_bot.workers.billing import issue

pytestmark = pytest.mark.integration


async def test_crypto_invoice_and_signed_duplicate_webhook(rt):
    rt.cfg.crypto_sales_enabled = True  # Isolated test transport; never a live payment.
    rt.bot.session.make_request = AsyncMock(return_value=True)
    async with rt.sessions.begin() as s:
        s.add(
            Setting(
                key="payments", value={"crypto_enabled": True, "min_topup": "1", "max_topup": "100"}
            )
        )
    async with rt.sessions.begin() as s:
        invoice = await payments.create_invoice(s, rt.cfg, 2, "crypto", Decimal(5))
    remote = {
        "invoice_id": 42,
        "payload": str(invoice.id),
        "currency_type": "fiat",
        "fiat": "USD",
        "amount": "5",
        "status": "paid",
        "bot_invoice_url": "https://t.me/CryptoBot?start=test-only",
    }

    async def provider(request):
        body = json.loads(request.content)
        if request.url.path.endswith("createInvoice"):
            assert body["currency_type"] == "fiat" and body["fiat"] == "USD"
            assert "accepted_assets" not in body  # provider's full available list
            return httpx.Response(200, json={"ok": True, "result": {**remote, "status": "active"}})
        return httpx.Response(200, json={"ok": True, "result": {"items": [remote]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
        rt.crypto.client = provider_client
        await issue(rt, invoice.id)
        body = json.dumps({"update_type": "invoice_paid", "payload": {"invoice_id": 42}}).encode()
        signature = hmac.new(
            hashlib.sha256(rt.cfg.crypto_token.get_secret_value().encode()).digest(),
            body,
            hashlib.sha256,
        ).hexdigest()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(rt)), base_url="https://test"
        ) as client:
            assert (await client.post("/webhooks/crypto", content=body)).status_code == 403
            for _ in range(3):
                response = await client.post(
                    "/webhooks/crypto",
                    content=body,
                    headers={"crypto-pay-api-signature": signature},
                )
                assert response.status_code == 200
    async with rt.sessions() as s:
        assert (await s.get(Wallet, 2)).balance == 15
        assert (await s.get(Invoice, invoice.id)).status == "paid"
