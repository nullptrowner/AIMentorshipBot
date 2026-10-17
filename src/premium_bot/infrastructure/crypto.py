import httpx

from premium_bot.domain import BusinessError, Json


class CryptoPay:
    def __init__(self, token: str, testnet: bool, client: httpx.AsyncClient) -> None:
        self.token, self.client = token, client
        self.base = (
            "https://testnet-pay.crypt.bot/api/" if testnet else "https://pay.crypt.bot/api/"
        )

    async def request(self, method: str, data: Json | None = None) -> Json:
        if not self.token:
            raise BusinessError("Crypto Pay ещё не настроен.")
        response = await self.client.post(
            self.base + method,
            json=data or {},
            headers={"Crypto-Pay-API-Token": self.token},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise BusinessError("Crypto Pay отклонил запрос. Обратитесь к администратору.")
        result = payload.get("result")
        return result if isinstance(result, dict) else {"items": result}

    async def create(self, amount: str, payload: str) -> Json:
        # USD-denominated invoice; provider's default accepted_assets includes all supported assets.
        return await self.request(
            "createInvoice",
            {
                "currency_type": "fiat",
                "fiat": "USD",
                "amount": amount,
                "payload": payload,
                "expires_in": 3600,
                "allow_comments": False,
            },
        )

    async def get(self, invoice_id: str) -> Json | None:
        data = await self.request("getInvoices", {"invoice_ids": invoice_id})
        items = data.get("items", [])
        return dict(items[0]) if items else None
