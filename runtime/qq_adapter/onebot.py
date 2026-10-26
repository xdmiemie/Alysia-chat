"""Small, model-independent OneBot v11 forward WebSocket client.

The transport makes no attempt to launch QQ, log in, or manage SnowLuma.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.legacy.client import WebSocketClientProtocol, connect

LOGGER = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    SNOWLUMA_UNREACHABLE = "SNOWLUMA_UNREACHABLE"
    ONEBOT_NOT_READY = "ONEBOT_NOT_READY"
    QQ_NOT_LOGGED_IN = "QQ_NOT_LOGGED_IN"
    READY = "READY"


STATE_HINTS = {
    ConnectionState.SNOWLUMA_UNREACHABLE: "请启动 SnowLuma，并检查本机地址和端口。",
    ConnectionState.ONEBOT_NOT_READY: "请检查 SnowLuma 的正向 WebSocket、Token 和账号挂载状态。",
    ConnectionState.QQ_NOT_LOGGED_IN: "请在 QQ 客户端手动登录，再在 SnowLuma 中确认账号在线。",
    ConnectionState.READY: "OneBot 与 QQ 账号均已就绪。",
}


@dataclass(frozen=True)
class OneBotConfig:
    ws_url: str = "ws://127.0.0.1:3001/"
    token: str = ""
    http_port: int = 3000
    connect_timeout: float = 5.0
    response_timeout: float = 5.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.ws_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("ws_url must be an absolute ws:// or wss:// URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("ws_url must not contain credentials, query, or fragment")
        if self.http_port not in range(1, 65536):
            raise ValueError("http_port must be between 1 and 65535")
        if self.connect_timeout <= 0 or self.response_timeout <= 0:
            raise ValueError("timeouts must be positive")


@dataclass(frozen=True)
class OneBotMessage:
    message_type: str
    user_id: int
    group_id: int | None
    message_id: int | str
    segments: tuple[dict[str, Any], ...]
    self_id: int | None = None
    sender_name: str = ""
    time: int | None = None
    sub_type: str = ""
    raw_message: str = ""

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> OneBotMessage | None:
        if event.get("post_type") != "message":
            return None
        kind = event.get("message_type")
        if kind not in {"private", "group"}:
            raise ValueError("unsupported message_type")
        user_id = int(event["user_id"])
        group_id = int(event["group_id"]) if kind == "group" else None
        message_id = event["message_id"]
        if not isinstance(message_id, (int, str)):
            raise ValueError("message_id must be an integer or string")
        message = event.get("message", [])
        if isinstance(message, str):
            segments = ({"type": "text", "data": {"text": message}},)
        elif isinstance(message, list) and all(isinstance(item, dict) for item in message):
            segments = tuple(message)
        else:
            raise ValueError("message must be text or a segment list")
        sender = event.get("sender") or {}
        if not isinstance(sender, dict):
            sender = {}
        return cls(
            kind, user_id, group_id, message_id, segments,
            int(event["self_id"]) if event.get("self_id") else None,
            str(sender.get("card") or sender.get("nickname") or ""),
            int(event["time"]) if event.get("time") is not None else None,
            str(event.get("sub_type") or ""),
            str(event.get("raw_message") or ""),
        )


@dataclass(frozen=True)
class ProbeResult:
    state: ConnectionState
    detail: str
    user_id: int | None = None

    @property
    def hint(self) -> str:
        return STATE_HINTS[self.state]


class OneBotActionError(RuntimeError):
    def __init__(self, action: str, retcode: Any) -> None:
        self.action = action
        self.retcode = retcode
        super().__init__(f"OneBot action {action} failed (retcode={retcode})")


class OneBotClient:
    def __init__(self, config: OneBotConfig) -> None:
        self.config = config
        self._socket: WebSocketClientProtocol | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._messages: asyncio.Queue[OneBotMessage | BaseException] = asyncio.Queue(maxsize=100)
        self._closed = asyncio.Event()

    async def __aenter__(self) -> OneBotClient:
        headers = {"Authorization": f"Bearer {self.config.token}"} if self.config.token else None
        self._socket = await connect(
            self.config.ws_url,
            extra_headers=headers,
            open_timeout=self.config.connect_timeout,
            ping_interval=20,
            ping_timeout=20,
        )
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._socket is not None:
            await self._socket.close()
        if self._reader is not None:
            await self._reader

    async def _read_loop(self) -> None:
        error: BaseException = ConnectionError("OneBot WebSocket disconnected")
        try:
            assert self._socket is not None
            async for raw in self._socket:
                try:
                    packet = json.loads(raw)
                    if not isinstance(packet, dict):
                        raise ValueError("packet must be an object")
                    if "echo" in packet:
                        pending = self._pending.get(str(packet["echo"]))
                        if pending is not None and not pending.done():
                            pending.set_result(packet)
                        continue
                    message = OneBotMessage.from_event(packet)
                    if message is not None:
                        self._put_message(message)
                except (ValueError, KeyError, TypeError) as exc:
                    LOGGER.warning("Ignoring malformed OneBot packet: %s", type(exc).__name__)
        except ConnectionClosed:
            pass
        except Exception as exc:
            error = ConnectionError(f"OneBot reader stopped: {type(exc).__name__}")
        finally:
            self._closed.set()
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(error)
            self._put_message(error)

    def _put_message(self, item: OneBotMessage | BaseException) -> None:
        if self._messages.full():
            self._messages.get_nowait()
            LOGGER.warning("OneBot message queue full; oldest event dropped")
        self._messages.put_nowait(item)

    async def next_message(self, timeout: float | None = None) -> OneBotMessage:
        if self._closed.is_set() and self._messages.empty():
            raise ConnectionError("OneBot WebSocket disconnected")
        item = await asyncio.wait_for(self._messages.get(), timeout=timeout)
        if isinstance(item, BaseException):
            raise item
        return item

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        if self._socket is None or self._closed.is_set():
            raise ConnectionError("OneBot WebSocket is not connected")
        echo = uuid.uuid4().hex
        pending: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = pending
        try:
            await self._socket.send(json.dumps({"action": action, "params": params or {}, "echo": echo}))
            packet = await asyncio.wait_for(pending, timeout=self.config.response_timeout)
            if packet.get("status") != "ok" or packet.get("retcode") != 0:
                raise OneBotActionError(action, packet.get("retcode"))
            return packet.get("data")
        finally:
            self._pending.pop(echo, None)

    async def send_private_message(self, user_id: int, text: str) -> Any:
        if user_id <= 0 or not text:
            raise ValueError("user_id must be positive and text must not be empty")
        return await self.call_api("send_private_msg", {"user_id": user_id, "message": text})

    async def send_group_message(self, group_id: int, text: str) -> Any:
        if group_id <= 0 or not text:
            raise ValueError("group_id must be positive and text must not be empty")
        return await self.call_api("send_group_msg", {"group_id": group_id, "message": text})


async def _http_port_open(config: OneBotConfig) -> bool:
    host = urlsplit(config.ws_url).hostname
    assert host is not None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, config.http_port), timeout=config.connect_timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def probe_connection(config: OneBotConfig) -> ProbeResult:
    try:
        async with OneBotClient(config) as client:
            try:
                status = await client.call_api("get_status")
                if not isinstance(status, dict):
                    return ProbeResult(ConnectionState.ONEBOT_NOT_READY, "Invalid status response")
                if status.get("online") is not True:
                    return ProbeResult(ConnectionState.QQ_NOT_LOGGED_IN, "QQ account is offline")
                login = await client.call_api("get_login_info")
            except (OneBotActionError, asyncio.TimeoutError, ConnectionError) as exc:
                return ProbeResult(ConnectionState.ONEBOT_NOT_READY, type(exc).__name__)
            if not isinstance(login, dict):
                return ProbeResult(ConnectionState.ONEBOT_NOT_READY, "Invalid status response")
            user_id = login.get("user_id")
            if not user_id:
                return ProbeResult(ConnectionState.QQ_NOT_LOGGED_IN, "QQ account is offline")
            if status.get("good") is not True:
                return ProbeResult(ConnectionState.ONEBOT_NOT_READY, "QQ link is not healthy")
            return ProbeResult(ConnectionState.READY, "QQ account and OneBot link are ready", int(user_id))
    except (OSError, asyncio.TimeoutError, InvalidHandshake, ConnectionClosed):
        if await _http_port_open(config):
            return ProbeResult(ConnectionState.ONEBOT_NOT_READY, "SnowLuma HTTP port responds; WebSocket does not")
        return ProbeResult(ConnectionState.SNOWLUMA_UNREACHABLE, "SnowLuma ports are unreachable")
