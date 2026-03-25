"""Async webhook delivery with HMAC-SHA256 signatures and retry."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .models import WebhookRecord

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 1.0  # seconds; attempt n waits backoff_base * 2^(n-1)


def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Return True if signature matches (or secret is empty → skip check)."""
    if not secret:
        return True
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected, header)


async def deliver_to_webhook(
    webhook: "WebhookRecord",
    event_payload: dict,
) -> bool:
    """Attempt delivery with exponential backoff. Returns True on success."""
    body = json.dumps(event_payload, default=str).encode()
    sig = compute_signature(webhook.secret, body) if webhook.secret else "sha256="
    headers = {
        "Content-Type": "application/json",
        "X-Pasloe-Signature": sig,
    }

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(webhook.url, content=body, headers=headers)
            if resp.status_code < 500:
                logger.debug(
                    "Webhook %s delivered (status=%d, attempt=%d)",
                    webhook.id, resp.status_code, attempt,
                )
                return True
            logger.warning(
                "Webhook %s got %d on attempt %d",
                webhook.id, resp.status_code, attempt,
            )
        except Exception as exc:
            logger.warning(
                "Webhook %s delivery error on attempt %d: %s",
                webhook.id, attempt, exc,
            )
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))

    logger.error("Webhook %s delivery failed after %d attempts", webhook.id, _MAX_ATTEMPTS)
    return False


async def fire_webhooks(webhooks: list["WebhookRecord"], event_payload: dict) -> bool:
    """Deliver to all matching webhooks. Returns True only if all succeeded."""
    if not webhooks:
        return True

    results = await asyncio.gather(
        *(deliver_to_webhook(wh, event_payload) for wh in webhooks),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            return False
        if result is not True:
            return False
    return True
