import time

import httpx


class JWKSCache:
    """Fetches and caches JWKS documents keyed by URL, with a TTL.

    Keyed by the effective (backchannel-aware) JWKS URL so different instances /
    backchannels never collide.
    """

    def __init__(self, client: httpx.AsyncClient, ttl_seconds: float):
        self._client = client
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, dict]]] = {}

    async def get_key(self, jwks_url: str, kid: str) -> dict | None:
        keys = await self._get_keys(jwks_url)
        key = keys.get(kid)
        if key is None:
            # kid unknown — force a refresh in case of key rotation, then retry once.
            keys = await self._get_keys(jwks_url, force=True)
            key = keys.get(kid)
        return key

    async def _get_keys(self, jwks_url: str, force: bool = False) -> dict[str, dict]:
        now = time.monotonic()
        cached = self._cache.get(jwks_url)
        if not force and cached is not None and (now - cached[0]) < self._ttl:
            return cached[1]
        resp = await self._client.get(jwks_url)
        resp.raise_for_status()
        keys = {k["kid"]: k for k in resp.json().get("keys", []) if "kid" in k}
        self._cache[jwks_url] = (now, keys)
        return keys
