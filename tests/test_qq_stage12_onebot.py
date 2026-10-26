"""OneBot transport checks against a local mock server, with no QQ account."""

from __future__ import annotations

import asyncio
import json

from websockets.legacy.server import serve

from runtime.qq_adapter import ConnectionState, OneBotClient, OneBotConfig, probe_connection
from runtime.qq_adapter.onebot import OneBotActionError


def run(coro):
    return asyncio.run(coro)


async def mock_server(handler):
    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/"


def test_twenty_messages_and_echo_correlation():
    async def scenario():
        async def handler(socket):
            requests = [json.loads(await socket.recv()) for _ in range(2)]
            assert requests[0]["echo"] != requests[1]["echo"]
            for request in reversed(requests):
                await socket.send(json.dumps({
                    "status": "ok", "retcode": 0,
                    "data": {"action": request["action"]}, "echo": request["echo"],
                }))
            for index in range(20):
                await socket.send(json.dumps({
                    "post_type": "message", "message_type": "group" if index % 2 else "private",
                    "user_id": 123, "group_id": 456, "message_id": index,
                    "message": [{"type": "text", "data": {"text": str(index)}}],
                }))

        server, url = await mock_server(handler)
        try:
            async with OneBotClient(OneBotConfig(ws_url=url)) as client:
                first, second = await asyncio.gather(
                    client.call_api("first"), client.call_api("second")
                )
                assert first == {"action": "first"}
                assert second == {"action": "second"}
                messages = [await client.next_message(timeout=1) for _ in range(20)]
                assert [item.message_id for item in messages] == list(range(20))
                assert messages[0].group_id is None
                assert messages[1].group_id == 456
                assert messages[0].segments[0]["data"]["text"] == "0"
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_timeout_action_failure_and_disconnect():
    async def scenario():
        async def handler(socket):
            while True:
                try:
                    request = json.loads(await socket.recv())
                except Exception:
                    return
                if request["action"] == "timeout":
                    continue
                if request["action"] == "failed":
                    await socket.send(json.dumps({
                        "status": "failed", "retcode": 1404,
                        "data": None, "echo": request["echo"],
                    }))
                if request["action"] == "disconnect":
                    await socket.close()

        server, url = await mock_server(handler)
        try:
            async with OneBotClient(OneBotConfig(ws_url=url, response_timeout=0.1)) as client:
                try:
                    await client.call_api("timeout")
                    assert False, "timeout expected"
                except asyncio.TimeoutError:
                    pass
                assert client._pending == {}
                try:
                    await client.call_api("failed")
                    assert False, "action failure expected"
                except OneBotActionError as exc:
                    assert exc.retcode == 1404
                try:
                    await client.call_api("disconnect")
                    assert False, "disconnect expected"
                except ConnectionError:
                    pass
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_diagnosis_offline_and_ready():
    async def scenario(online):
        async def handler(socket):
            assert socket.request_headers["Authorization"] == "Bearer test-token"
            async for raw in socket:
                request = json.loads(raw)
                data = {"user_id": 123, "nickname": "test"} if request["action"] == "get_login_info" else {"online": online, "good": online}
                await socket.send(json.dumps({
                    "status": "ok", "retcode": 0, "data": data, "echo": request["echo"],
                }))

        server, url = await mock_server(handler)
        try:
            return await probe_connection(OneBotConfig(ws_url=url, token="test-token"))
        finally:
            server.close()
            await server.wait_closed()

    assert run(scenario(False)).state is ConnectionState.QQ_NOT_LOGGED_IN
    ready = run(scenario(True))
    assert ready.state is ConnectionState.READY
    assert ready.user_id == 123


def test_websocket_not_ready_while_http_port_is_open():
    async def scenario():
        async def handler(reader, writer):
            await reader.read(1024)
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            result = await probe_connection(OneBotConfig(
                ws_url=f"ws://127.0.0.1:{port}/", http_port=port, connect_timeout=0.5
            ))
            assert result.state is ConnectionState.ONEBOT_NOT_READY
        finally:
            server.close()
            await server.wait_closed()

    run(scenario())


def test_unreachable_service():
    async def scenario():
        server, url = await mock_server(lambda socket: None)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        return await probe_connection(OneBotConfig(ws_url=url, http_port=port, connect_timeout=0.1))

    assert run(scenario()).state is ConnectionState.SNOWLUMA_UNREACHABLE
