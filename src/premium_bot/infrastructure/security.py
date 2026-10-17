import base64
import hashlib
import hmac
import ipaddress
import socket
import time
from urllib.parse import urlsplit

from premium_bot.domain import BusinessError


def verify_replicate(
    body: bytes, headers: dict[str, str], secret: str, clock: int | None = None
) -> bool:
    try:
        if not secret.startswith("whsec_"):
            return False
        stamp = headers["webhook-timestamp"]
        if abs((clock if clock is not None else int(time.time())) - int(stamp)) > 300:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        if not key:
            return False
        signed = headers["webhook-id"].encode() + b"." + stamp.encode() + b"." + body
        expected = base64.b64encode(hmac.digest(key, signed, "sha256")).decode()
        return any(
            hmac.compare_digest(part.removeprefix("v1,"), expected)
            for part in headers["webhook-signature"].split()
            if part.startswith("v1,")
        )
    except (KeyError, ValueError):
        return False


def verify_crypto(body: bytes, signature: str, token: str) -> bool:
    if not token or not signature:
        return False
    key = hashlib.sha256(token.encode()).digest()
    return hmac.compare_digest(hmac.new(key, body, hashlib.sha256).hexdigest(), signature)


def media_signature(file_id: str, expires: int, secret: str) -> str:
    return hmac.new(secret.encode(), f"{file_id}:{expires}".encode(), hashlib.sha256).hexdigest()


def prediction_binding(job_id: str, secret: str) -> str:
    return hmac.new(secret.encode(), ("prediction:" + job_id).encode(), hashlib.sha256).hexdigest()


def verify_media(file_id: str, expires: int, signature: str, secret: str) -> bool:
    return (
        bool(secret)
        and int(time.time()) <= expires <= int(time.time()) + 7200
        and hmac.compare_digest(media_signature(file_id, expires, secret), signature)
    )


def check_download_url(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise BusinessError("Небезопасный адрес результата.")
    if not (host == "replicate.delivery" or host.endswith(".replicate.delivery")):
        raise BusinessError("Адрес результата вне разрешённого хранилища Replicate.")
    return host, 443


def resolve_public(host: str, port: int) -> None:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise BusinessError("Скачивание из приватной сети запрещено.")
