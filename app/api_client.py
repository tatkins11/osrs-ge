"""Client for the OSRS Wiki Real-time Prices API.

Endpoints (base https://prices.runescape.wiki/api/v1/osrs):
  /mapping        static item metadata (id, name, members, limit, value, alch, icon)
  /latest         most recent insta-buy/instasell per item (high/low + times)
  /5m, /1h        rolling averages with traded volume per side
  /timeseries     up to ~365 points for a single item at a given timestep
"""
from __future__ import annotations

import logging
import os
import ssl
import time
from typing import Any

import httpx

from .config import API_BASE, HTTP_RETRIES, HTTP_TIMEOUT, USER_AGENT

log = logging.getLogger(__name__)

VALID_TIMESTEPS = {"5m", "1h", "6h", "24h"}


def _ssl_verify():
    """Resolve TLS verification.

    Corporate networks often run a TLS-inspecting proxy whose root CA lives in
    the Windows trust store but not in Python's bundled CA list. ``truststore``
    makes Python verify against the OS store so verification stays ON. Escape
    hatches: OSRS_GE_CA_BUNDLE=<pem path>, or OSRS_GE_INSECURE_SSL=1 to disable.
    """
    ca = os.getenv("OSRS_GE_CA_BUNDLE")
    if ca:
        return ca
    if os.getenv("OSRS_GE_INSECURE_SSL", "").lower() in {"1", "true", "yes"}:
        log.warning("TLS verification DISABLED via OSRS_GE_INSECURE_SSL")
        return False
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception as e:  # pragma: no cover - fallback to bundled CAs
        log.warning("truststore unavailable (%s); using default CA bundle", e)
        return True


class OsrsPricesClient:
    """Thin, polite HTTP client with retry/backoff over the prices API."""

    def __init__(
        self,
        base_url: str = API_BASE,
        user_agent: str = USER_AGENT,
        timeout: float = HTTP_TIMEOUT,
        retries: int = HTTP_RETRIES,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout,
            verify=_ssl_verify(),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OsrsPricesClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low level -----------------------------------------------------------
    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_err: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"status {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_err = e
                wait = min(2**attempt, 30)
                log.warning(
                    "GET %s failed (attempt %d/%d): %s -- retrying in %ss",
                    path, attempt + 1, self.retries, e, wait,
                )
                time.sleep(wait)
        raise RuntimeError(f"GET {path} failed after {self.retries} attempts") from last_err

    # -- endpoints -----------------------------------------------------------
    def get_mapping(self) -> list[dict]:
        """Static item metadata. Returns a list of item dicts."""
        return self._get("mapping")

    def get_latest(self) -> dict[int, dict]:
        """Latest insta-buy/instasell per item: {id: {high, highTime, low, lowTime}}."""
        data = self._get("latest").get("data", {})
        return {int(k): v for k, v in data.items()}

    def get_5m(self, timestamp: int | None = None) -> dict[int, dict]:
        """5-minute averages: {id: {avgHighPrice, highPriceVolume, avgLowPrice, lowPriceVolume}}."""
        params = {"timestamp": timestamp} if timestamp else None
        return {int(k): v for k, v in self._get("5m", params).get("data", {}).items()}

    def get_1h(self, timestamp: int | None = None) -> dict[int, dict]:
        """1-hour averages, same shape as get_5m."""
        params = {"timestamp": timestamp} if timestamp else None
        return {int(k): v for k, v in self._get("1h", params).get("data", {}).items()}

    def get_timeseries(self, item_id: int, timestep: str = "5m") -> list[dict]:
        """Up to ~365 points for one item. timestep in {5m, 1h, 6h, 24h}."""
        if timestep not in VALID_TIMESTEPS:
            raise ValueError(f"invalid timestep {timestep!r}; use one of {VALID_TIMESTEPS}")
        return self._get("timeseries", {"id": item_id, "timestep": timestep}).get("data", [])
