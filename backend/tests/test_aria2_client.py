import pytest
import respx
from httpx import Response
from app.aria2_client import Aria2Client


@pytest.mark.asyncio
@respx.mock
async def test_add_uri_and_status():
    route = respx.post("http://aria2.test/jsonrpc").mock(side_effect=[
        Response(200, json={"id": "1", "jsonrpc": "2.0", "result": "gid-1"}),
        Response(200, json={"id": "2", "jsonrpc": "2.0", "result": {
            "status": "complete",
            "completedLength": "100",
            "totalLength": "100",
            "downloadSpeed": "0",
        }}),
    ])
    client = Aria2Client("http://aria2.test/jsonrpc", secret="token")
    gid = await client.add_uri(["https://example.com/f.bin"], "/tmp", "f.bin", 16)
    assert gid == "gid-1"
    st = await client.tell_status(gid)
    assert st["status"] == "complete"
    assert st["completed_length"] == 100
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_force_remove():
    route = respx.post("http://aria2.test/jsonrpc").mock(
        return_value=Response(200, json={"id": "1", "jsonrpc": "2.0", "result": "ok"})
    )
    client = Aria2Client("http://aria2.test/jsonrpc", secret="token")
    await client.force_remove("gid-1")
    assert route.called
    body = route.calls.last.request.content
    assert b"aria2.forceRemove" in body
    assert b"gid-1" in body
