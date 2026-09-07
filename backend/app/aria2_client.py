import uuid

import httpx


class Aria2Client:
    def __init__(
        self,
        rpc_url: str,
        *,
        secret: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._rpc_url = rpc_url
        self._secret = secret
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _auth_params(self, *args: object) -> list[object]:
        if self._secret:
            return [f"token:{self._secret}", *args]
        return list(args)

    async def _call(self, method: str, params: list[object]) -> object:
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        resp = await self._client.post(self._rpc_url, json=body)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]

    async def is_available(self) -> bool:
        try:
            await self._call("aria2.getVersion", self._auth_params())
            return True
        except Exception:
            return False

    async def add_uri(
        self,
        uris: list[str],
        out_dir: str,
        out_name: str,
        connections: int,
    ) -> str:
        options = {
            "dir": out_dir,
            "out": out_name,
            "split": connections,
            "max-connection-per-server": connections,
            "continue": "true",
        }
        result = await self._call(
            "aria2.addUri",
            self._auth_params(uris, options),
        )
        return str(result)

    async def tell_status(self, gid: str) -> dict:
        result = await self._call("aria2.tellStatus", self._auth_params(gid))
        return {
            "status": result["status"],
            "completed_length": int(result["completedLength"]),
            "total_length": int(result["totalLength"]),
            "download_speed": int(result["downloadSpeed"]),
        }

    async def force_remove(self, gid: str) -> None:
        await self._call("aria2.forceRemove", self._auth_params(gid))
