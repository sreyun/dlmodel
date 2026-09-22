import uuid

import httpx

# Fast probe so an unreachable aria2 falls back to HTTP quickly instead of
# stalling on the client's default (longer) read/connect timeouts.
_PROBE_TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=5.0)


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
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _auth_params(self, *args: object) -> list[object]:
        if self._secret:
            return [f"token:{self._secret}", *args]
        return list(args)

    async def _call(
        self, method: str, params: list[object], *, timeout: httpx.Timeout | None = None
    ) -> object:
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        # Only pass timeout when set: httpx treats an explicit ``None`` as
        # "no timeout" rather than "use the client default".
        if timeout is not None:
            resp = await self._client.post(self._rpc_url, json=body, timeout=timeout)
        else:
            resp = await self._client.post(self._rpc_url, json=body)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            if isinstance(err, dict):
                raise RuntimeError(err.get("message") or str(err))
            raise RuntimeError(str(err))
        return data["result"]

    async def is_available(self) -> bool:
        try:
            await self._call(
                "aria2.getVersion", self._auth_params(), timeout=_PROBE_TIMEOUT
            )
            return True
        except Exception:
            return False

    async def add_uri(
        self,
        uris: list[str],
        out_dir: str,
        out_name: str,
        connections: int,
        *,
        headers: list[str] | None = None,
        max_tries: int = 5,
        file_allocation: str = "none",
    ) -> str:
        # Cap per-server connections — too many TLS streams amplify mirror EOF.
        per_server = max(1, min(int(connections), 8))
        options: dict[str, object] = {
            "dir": out_dir,
            "out": out_name,
            "split": str(per_server),
            "max-connection-per-server": str(per_server),
            "continue": "true",
            "max-tries": str(max(1, int(max_tries))),
            "retry-wait": "3",
            "connect-timeout": "30",
            "timeout": "120",
            # aria2-pro ships ``file-allocation=falloc``, which Docker Desktop's
            # Windows-backed mounts (and most network filesystems) reject outright:
            # every >64MB file died with "fallocate failed. Operation not supported"
            # and the whole download silently degraded to single-stream HTTP.
            # Per-URI options win over the daemon config, and ``none`` is aria2's own
            # default, so no host filesystem can be worse off than upstream.
            "file-allocation": file_allocation,
        }
        if headers:
            options["header"] = list(headers)
        result = await self._call(
            "aria2.addUri",
            self._auth_params(uris, options),
        )
        return str(result)

    async def tell_status(self, gid: str) -> dict:
        result = await self._call(
            "aria2.tellStatus",
            self._auth_params(
                gid,
                ["status", "completedLength", "totalLength", "downloadSpeed", "errorMessage"],
            ),
        )
        return {
            "status": result["status"],
            "completed_length": int(result["completedLength"]),
            "total_length": int(result["totalLength"]),
            "download_speed": int(result["downloadSpeed"]),
            "error_message": result.get("errorMessage") or "",
        }

    async def force_remove(self, gid: str) -> None:
        await self._call("aria2.forceRemove", self._auth_params(gid))
