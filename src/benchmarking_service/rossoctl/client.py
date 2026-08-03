import httpx


class RossoctlError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RossoctlClient:
    """Thin wrapper over the Rossoctl HTTP API, bearing a fresh Service JWT.

    The token is the Service's own per-request ROPC token, never the caller's.
    """

    def __init__(self, base_url: str, service_token: str, client: httpx.AsyncClient):
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._headers = {"Authorization": f"Bearer {service_token}"}

    async def namespaces(self) -> list:
        resp = await self._client.get(
            f"{self._base_url}/api/v1/namespaces", headers=self._headers
        )
        if resp.status_code != 200:
            raise RossoctlError(
                f"GET /api/v1/namespaces failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
            )
        data = resp.json()
        # Backend may return a bare list or wrap it; normalize to a list.
        if isinstance(data, dict):
            return data.get("namespaces", data.get("items", []))
        return data

    async def list_agents(self, namespace: str | None = None) -> list:
        params = {"namespace": namespace} if namespace else None
        resp = await self._client.get(
            f"{self._base_url}/api/v1/agents", headers=self._headers, params=params
        )
        if resp.status_code != 200:
            raise RossoctlError(
                f"GET /api/v1/agents failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
            )
        data = resp.json()
        return data.get("items", []) if isinstance(data, dict) else data

    async def get_agent(self, namespace: str, name: str) -> dict:
        resp = await self._client.get(
            f"{self._base_url}/api/v1/agents/{namespace}/{name}", headers=self._headers
        )
        if resp.status_code != 200:
            raise RossoctlError(
                f"GET agent {namespace}/{name} failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
            )
        return resp.json()

    async def create_agent(self, body: dict) -> dict:
        resp = await self._client.post(
            f"{self._base_url}/api/v1/agents", headers=self._headers, json=body
        )
        if resp.status_code not in (200, 201):
            raise RossoctlError(
                f"POST /api/v1/agents failed (HTTP {resp.status_code}): {resp.text[:300]}",
                status_code=resp.status_code,
            )
        return resp.json() if resp.content else {}

    async def delete_agent(self, namespace: str, name: str) -> None:
        resp = await self._client.delete(
            f"{self._base_url}/api/v1/agents/{namespace}/{name}", headers=self._headers
        )
        if resp.status_code not in (200, 202, 204):
            raise RossoctlError(
                f"DELETE agent {namespace}/{name} failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
            )

    async def list_tools(self, namespace: str | None = None) -> list:
        params = {"namespace": namespace} if namespace else None
        resp = await self._client.get(
            f"{self._base_url}/api/v1/tools", headers=self._headers, params=params
        )
        if resp.status_code != 200:
            raise RossoctlError(
                f"GET /api/v1/tools failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
            )
        data = resp.json()
        return data.get("items", []) if isinstance(data, dict) else data

    async def get_tool(self, namespace: str, name: str) -> dict:
        resp = await self._client.get(
            f"{self._base_url}/api/v1/tools/{namespace}/{name}", headers=self._headers
        )
        if resp.status_code != 200:
            raise RossoctlError(
                f"GET tool {namespace}/{name} failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
            )
        return resp.json()

    async def create_tool(self, body: dict) -> dict:
        resp = await self._client.post(
            f"{self._base_url}/api/v1/tools", headers=self._headers, json=body
        )
        if resp.status_code not in (200, 201):
            raise RossoctlError(
                f"POST /api/v1/tools failed (HTTP {resp.status_code}): {resp.text[:300]}",
                status_code=resp.status_code,
            )
        return resp.json() if resp.content else {}

    async def delete_tool(self, namespace: str, name: str) -> None:
        resp = await self._client.delete(
            f"{self._base_url}/api/v1/tools/{namespace}/{name}", headers=self._headers
        )
        if resp.status_code not in (200, 202, 204):
            raise RossoctlError(
                f"DELETE tool {namespace}/{name} failed (HTTP {resp.status_code})",
                status_code=resp.status_code,
            )
