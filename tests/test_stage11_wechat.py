from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from runtime.adapters.wechat.config import WeChatConfig, load_wechat_config
from runtime.adapters.wechat.ilink.api import ILinkApiClient, ILinkApiError
from runtime.adapters.wechat.ilink.cursor_store import CursorStore
from runtime.adapters.wechat.ilink.event_parser import ILinkEventParser
from runtime.adapters.wechat.ilink.long_poller import ILinkLongPoller, LoginExpiredError
from runtime.adapters.wechat.ilink.sender import ILinkSender
from runtime.adapters.wechat.message_buffer import MessageBuffer
from runtime.adapters.wechat.models import ILinkCredentials, ILinkTextMessage, SendResult
from runtime.adapters.wechat.router import WeChatTextRouter
from runtime.core.models import AlysiaReply, CoreTiming, UnifiedMessage


class FakeCore:
    def __init__(self) -> None:
        self.messages: list[UnifiedMessage] = []

    def process(self, message: UnifiedMessage) -> AlysiaReply:
        self.messages.append(message)
        return AlysiaReply(
            request_id=message.request_id,
            session_id=message.session_id,
            channel=message.channel,
            ok=True,
            text=f"reply:{message.text}",
            message_type="text",
            audio_path="",
            state="COMPLETED",
            states=("ACCEPTED", "COMPLETED"),
            error_code="NONE",
            error="",
            transcription=None,
            reply=None,
            timings=CoreTiming(),
            metadata=dict(message.metadata),
        )


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def send_text(self, to_user_id: str, context_token: str, text: str) -> SendResult:
        self.calls.append((to_user_id, context_token, text))
        return SendResult(ok=True, message_id=f"sent-{len(self.calls)}")


def message(
    message_id: str = "m1",
    *,
    from_user_id: str = "allowed@im.wechat",
    context_token: str = "ctx-1",
    seq: str = "1",
    text: str = "hello",
    cursor: str = "buf-1",
) -> ILinkTextMessage:
    return ILinkTextMessage(
        message_id=message_id,
        seq=seq,
        from_user_id=from_user_id,
        session_id=f"session-{from_user_id}",
        context_token=context_token,
        text=text,
        create_time_ms=1000,
        get_updates_buf=cursor,
        account_id="main",
    )


def test_wechat_config_loads_nested_yaml_and_contact_policy():
    config = load_wechat_config("configs/runtime/wechat.yaml")

    assert config.provider == "ilink"
    assert config.ilink.credentials_path == Path("storage/secrets/ilink/account.json")
    assert config.reply.default_reply_mode == "text"
    assert config.contacts.is_allowed("example_user_id@im.wechat") is True
    assert config.contacts.is_allowed("stranger@im.wechat") is False
    assert config.contacts.block_user_ids == ()


def test_event_parser_ignores_bot_messages_and_extracts_text():
    parser = ILinkEventParser(account_id="main", bot_user_id="bot@im.wechat")
    payload = {
        "get_updates_buf": "next-cursor",
        "update_list": [
            {
                "message_type": "BOT",
                "from_user_id": "bot@im.wechat",
                "message_id": "self",
                "item_list": [{"type": "TEXT", "text_item": {"text": "self"}}],
            },
            {
                "message_type": "USER",
                "from_user_id": "allowed@im.wechat",
                "session_id": "s1",
                "message_id": "m1",
                "seq": 7,
                "context_token": "ctx-latest",
                "create_time_ms": 123,
                "item_list": [{"type": "TEXT", "text_item": {"text": "你好"}}],
            },
            {
                "message_type": "USER",
                "from_user_id": "allowed@im.wechat",
                "message_id": "voice",
                "item_list": [{"type": "VOICE", "voice_item": {}}],
            },
        ],
    }

    batch = parser.parse_update_batch(payload)

    assert batch.cursor == "next-cursor"
    assert len(batch.messages) == 1
    assert batch.messages[0].text == "你好"
    assert batch.messages[0].context_token == "ctx-latest"


def test_event_parser_accepts_openclaw_numeric_msg_shape():
    parser = ILinkEventParser(account_id="main", bot_user_id="bot@im.bot")

    batch = parser.parse_update_batch(
        {
            "ret": 0,
            "get_updates_buf": "next-cursor",
            "msgs": [
                {
                    "message_type": 1,
                    "from_user_id": "allowed@im.wechat",
                    "session_id": "s1",
                    "message_id": "m1",
                    "seq": 7,
                    "context_token": "ctx-latest",
                    "create_time_ms": 123,
                    "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                }
            ],
        }
    )

    assert len(batch.messages) == 1
    assert batch.messages[0].text == "你好"
    assert batch.messages[0].message_id == "m1"


def test_message_buffer_merges_only_same_user():
    buffer = MessageBuffer(buffer_seconds=1.5)

    assert buffer.add(message("a", text="one"), now=10.0) == []
    assert buffer.add(message("b", text="two", context_token="ctx-2"), now=10.5) == []
    assert buffer.add(message("c", from_user_id="other@im.wechat", text="other"), now=10.6) == []
    ready = buffer.flush_expired(now=12.0)

    assert len(ready) == 1
    assert ready[0].from_user_id == "allowed@im.wechat"
    assert ready[0].text == "one\ntwo"
    assert ready[0].latest.context_token == "ctx-2"
    assert buffer.flush_all()[0].from_user_id == "other@im.wechat"


def test_router_enforces_whitelist_dedup_and_uses_latest_context_token(tmp_path):
    config = WeChatConfig(log_path=tmp_path / "wechat.jsonl")
    config = config.__class__(
        **{
            **config.__dict__,
            "contacts": config.contacts.__class__(
                default_policy="deny",
                allow_user_ids=("allowed@im.wechat",),
                block_user_ids=(),
            ),
        }
    )
    core = FakeCore()
    sender = FakeSender()
    router = WeChatTextRouter(
        config=config,
        core=core,
        sender=sender,
        buffer=MessageBuffer(buffer_seconds=0),
    )

    asyncio.run(router.accept(message("m1", context_token="ctx-right", text="hello")))
    asyncio.run(router.accept(message("m1", context_token="ctx-right", text="hello")))
    asyncio.run(router.accept(message("m2", from_user_id="stranger@im.wechat", text="nope")))

    assert len(core.messages) == 1
    assert core.messages[0].channel == "wechat_ilink"
    assert core.messages[0].session_id == "wechat_ilink:main:allowed@im.wechat"
    assert sender.calls == [("allowed@im.wechat", "ctx-right", "reply:hello")]


class FakeTransport:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []
        self.fail_once = True

    def post_json(self, url, payload, headers, timeout):
        self.posts.append((url, payload))
        if self.fail_once:
            self.fail_once = False
            return {"errcode": 500, "errmsg": "temporary"}
        return {"errcode": 0, "message_id": "sent-1", "context_token": "must-not-log"}


def test_sender_retries_and_keeps_idempotent_success():
    transport = FakeTransport()
    api = ILinkApiClient(
        credentials=ILinkCredentials("token", "bot", "https://ilink.test"),
        bot_agent="agent",
        timeout_seconds=1,
        channel_version="2.1.7",
        transport=transport,
    )
    sender = ILinkSender(api, retry_delay_seconds=0, max_retries=1)

    first = asyncio.run(sender.send_text("allowed@im.wechat", "ctx", " hello "))
    second = asyncio.run(sender.send_text("allowed@im.wechat", "ctx", "hello"))

    assert first.ok is True
    assert second == first
    assert len(transport.posts) == 2
    assert transport.posts[0][0] == "https://ilink.test/ilink/bot/sendmessage"
    sent_msg = transport.posts[1][1]["msg"]
    assert sent_msg["from_user_id"] == ""
    assert sent_msg["message_type"] == 2
    assert sent_msg["message_state"] == 2
    assert sent_msg["client_id"].startswith("alysia-chat:")
    assert sent_msg["item_list"][0]["type"] == 1
    assert transport.posts[1][1]["base_info"]["channel_version"] == "2.1.7"
    assert "context_token" not in first.metadata


def test_api_client_uses_openclaw_ilink_getupdates_path_and_headers():
    class CaptureTransport:
        def __init__(self) -> None:
            self.url = ""
            self.payload = {}
            self.headers = {}

        def post_json(self, url, payload, headers, timeout):
            self.url = url
            self.payload = payload
            self.headers = headers
            return {"ret": 0, "get_updates_buf": "next", "msgs": []}

    transport = CaptureTransport()
    api = ILinkApiClient(
        credentials=ILinkCredentials("token", "bot", "https://ilink.test"),
        bot_agent="agent",
        timeout_seconds=1,
        channel_version="2.1.7",
        transport=transport,
    )

    asyncio.run(api.get_updates("cursor"))

    assert transport.url == "https://ilink.test/ilink/bot/getupdates"
    assert transport.payload["get_updates_buf"] == "cursor"
    assert transport.payload["base_info"]["channel_version"] == "2.1.7"
    assert transport.headers["AuthorizationType"] == "ilink_bot_token"
    assert transport.headers["Authorization"] == "Bearer token"


@dataclass
class PollApi:
    calls: int = 0

    async def get_updates(self, cursor: str):
        self.calls += 1
        if self.calls == 1:
            return {
                "get_updates_buf": "cursor-2",
                "update_list": [
                    {
                        "message_type": "USER",
                        "from_user_id": "allowed@im.wechat",
                        "message_id": "m1",
                        "seq": 1,
                        "context_token": "ctx",
                        "item_list": [{"type": "TEXT", "text_item": {"text": "hello"}}],
                    }
                ],
            }
        raise ILinkApiError("expired", retryable=False)


def test_long_poller_commits_cursor_after_parse_and_stops_on_login_expiry(tmp_path):
    poller = ILinkLongPoller(
        api=PollApi(),
        cursor_store=CursorStore(tmp_path / "cursor.txt"),
        parser=ILinkEventParser(account_id="main", bot_user_id="bot"),
        reconnect_seconds=0,
        max_reconnect_seconds=0,
    )

    async def run_once_then_expire():
        iterator = poller.messages()
        first = await anext(iterator)
        with pytest.raises(LoginExpiredError):
            await anext(iterator)
        return first

    messages = asyncio.run(run_once_then_expire())

    assert messages[0].text == "hello"
    assert (tmp_path / "cursor.txt").read_text(encoding="utf-8") == "cursor-2"
