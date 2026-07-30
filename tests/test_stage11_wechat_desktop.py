from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import runtime.adapters.wechat_desktop.client as wechat_client
from runtime.adapters.wechat_desktop.app import _reply_to_batch
from runtime.adapters.wechat_desktop.client import PyWeixinDesktopClient
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig, load_wechat_desktop_config
from runtime.adapters.wechat_desktop.deduplicator import MessageDeduplicator
from runtime.adapters.wechat_desktop.message_buffer import MessageBuffer
from runtime.adapters.wechat_desktop.message_monitor import MessageMonitor
from runtime.adapters.wechat_desktop.message_parser import MessageParser
from runtime.adapters.wechat_desktop.models import BufferedDesktopBatch, DesktopMessage, SendResult
from runtime.adapters.wechat_desktop.process_locator import find_wechat_processes
from runtime.adapters.wechat_desktop.health import check_wechat_desktop_health
from runtime.adapters.wechat_desktop.text_sender import WeChatDesktopSender
from runtime.adapters.wechat_desktop.ui_snapshot import ChatMessageSnapshot, ChatSessionSnapshot, UISnapshot
from runtime.adapters.wechat_desktop.window_locator import WindowInfo


class FakeWeChatDesktopClient:
    def __init__(self) -> None:
        self.current_contact = ""
        self.opened: list[str] = []
        self.list_sessions_calls = 0
        self.sent: list[tuple[str, str]] = []
        self.sessions = [
            ChatSessionSnapshot(
                contact_key="alice-key",
                display_name="Alice",
                title="Alice",
                unread=True,
                last_text="hello",
            )
        ]
        self.messages_by_contact = {
            "alice-key": [
                ChatMessageSnapshot(
                    direction="incoming",
                    message_type="text",
                    text="hello",
                    runtime_id="m1",
                    relative_order=1,
                    time_label="10:00",
                )
            ]
        }

    def locate_main_window(self):
        return WindowInfo(title="微信", handle="1", visible=True)

    def snapshot(self):
        return UISnapshot(
            logged_in=True,
            account_display_name="爱莉希雅",
            version="test",
            main_window_title="微信",
            controls={
                "chat_list": True,
                "chat_title": True,
                "message_list": True,
                "input_box": True,
                "send_button": True,
            },
        )

    def list_sessions(self, limit: int):
        self.list_sessions_calls += 1
        return tuple(self.sessions[:limit])

    def open_chat(self, contact_key: str):
        self.opened.append(contact_key)
        self.current_contact = contact_key

    def current_chat_title(self):
        return self.current_contact

    def recent_messages(self, limit: int = 8):
        return tuple(self.messages_by_contact.get(self.current_contact, [])[-limit:])

    def send_text(self, text: str, *, restore_clipboard: bool = True):
        self.sent.append((self.current_contact, text))
        self.messages_by_contact.setdefault(self.current_contact, []).append(
            ChatMessageSnapshot(direction="outgoing", message_type="text", text=text, runtime_id=f"sent-{len(self.sent)}")
        )

    def has_own_text_at_tail(self, text: str):
        messages = self.messages_by_contact.get(self.current_contact, [])
        return bool(messages and messages[-1].direction == "outgoing" and messages[-1].text == text)


def test_desktop_config_loads_stage11_defaults():
    config = load_wechat_desktop_config("configs/runtime/wechat.yaml")

    assert config.provider == "pyweixin"
    assert config.client.executable_names == (
        "Weixin.exe",
        "WeChat.exe",
        "WeChatAppEx.exe",
        "wechat_starter_v1.0.0.exe",
    )
    assert config.monitor.poll_interval_ms == 100
    assert config.monitor.buffer_seconds == 0
    assert config.contacts.is_allowed("nobody") is False
    assert config.contacts.allow_display_names == ("n", "L", "文件传输助手")
    assert config.contacts.is_allowed("n") is True
    assert config.contacts.is_allowed("L") is True
    assert config.contacts.is_allowed("文件传输助手") is True


def test_parser_accepts_only_reliable_incoming_text_shape():
    parser = MessageParser(account_key="role")
    session = ChatSessionSnapshot(contact_key="alice-key", display_name="Alice", title="Alice")
    incoming = parser.parse(
        session,
        ChatMessageSnapshot(
            direction="incoming",
            message_type="text",
            text="你好",
            runtime_id="m1",
            metadata={"fingerprint": "stable-uia-fingerprint"},
        ),
    )
    outgoing = parser.parse(
        session,
        ChatMessageSnapshot(direction="self", message_type="text", text="我发的", runtime_id="m2"),
    )

    assert incoming.direction == "incoming"
    assert incoming.message_type == "text"
    assert incoming.session_id == "wechat_desktop:role:alice-key"
    assert incoming.raw_fingerprint == "stable-uia-fingerprint"
    assert outgoing.direction == "outgoing"


def test_monitor_enforces_allowlist_deduplicates_and_buffers(tmp_path):
    client = FakeWeChatDesktopClient()
    config = WeChatDesktopConfig(
        log_path=tmp_path / "wechat.jsonl",
        dedup_path=tmp_path / "dedup.jsonl",
        contacts=WeChatDesktopConfig.contacts.__class__(
            default_policy="deny",
            allow_contact_keys=("alice-key",),
        ),
        monitor=WeChatDesktopConfig.monitor.__class__(buffer_seconds=0),
    )
    monitor = MessageMonitor(config=config, client=client)

    first = monitor.poll_once()
    second = monitor.poll_once()

    assert len(first) == 1
    assert first[0].text == "hello"
    assert second == []
    assert client.list_sessions_calls == 0
    assert "accepted_text" in (tmp_path / "wechat.jsonl").read_text(encoding="utf-8")


def test_monitor_streams_ready_batch_before_polling_next_configured_contact(tmp_path):
    client = FakeWeChatDesktopClient()
    client.messages_by_contact["bob-key"] = [
        ChatMessageSnapshot(
            direction="incoming",
            message_type="text",
            text="hi from bob",
            runtime_id="bob-1",
            relative_order=1,
            time_label="10:01",
        )
    ]
    config = WeChatDesktopConfig(
        log_path=tmp_path / "wechat.jsonl",
        dedup_path=tmp_path / "dedup.jsonl",
        contacts=WeChatDesktopConfig.contacts.__class__(
            default_policy="deny",
            allow_contact_keys=("alice-key", "bob-key"),
        ),
        monitor=WeChatDesktopConfig.monitor.__class__(buffer_seconds=0),
    )
    monitor = MessageMonitor(config=config, client=client)

    batches = monitor.iter_ready_batches()
    first = next(batches)

    assert first.contact_key == "alice-key"
    assert client.opened == ["alice-key"]

    remaining = list(batches)

    assert [batch.contact_key for batch in remaining] == ["bob-key"]
    assert client.opened == ["alice-key", "bob-key"]


def test_monitor_uses_latest_unseen_incoming_message(tmp_path):
    client = FakeWeChatDesktopClient()
    client.messages_by_contact["alice-key"] = [
        ChatMessageSnapshot(
            direction="incoming",
            message_type="text",
            text="old",
            runtime_id="old",
            metadata={"fingerprint": "old"},
        ),
        ChatMessageSnapshot(direction="outgoing", message_type="text", text="reply", runtime_id="reply"),
        ChatMessageSnapshot(
            direction="incoming",
            message_type="text",
            text="new",
            runtime_id="new",
            metadata={"fingerprint": "new"},
        ),
    ]
    config = WeChatDesktopConfig(
        log_path=tmp_path / "wechat.jsonl",
        dedup_path=tmp_path / "dedup.jsonl",
        contacts=WeChatDesktopConfig.contacts.__class__(
            default_policy="deny",
            allow_contact_keys=("alice-key",),
        ),
        monitor=WeChatDesktopConfig.monitor.__class__(buffer_seconds=0),
    )
    monitor = MessageMonitor(config=config, client=client)

    ready = monitor.poll_once()

    assert len(ready) == 1
    assert ready[0].text == "new"


def test_monitor_does_not_reply_again_when_outgoing_is_newer_than_incoming(tmp_path):
    client = FakeWeChatDesktopClient()
    client.messages_by_contact["alice-key"] = [
        ChatMessageSnapshot(
            direction="incoming",
            message_type="text",
            text="remember blue",
            runtime_id="incoming-1",
            metadata={"fingerprint": "unstable-first"},
        ),
        ChatMessageSnapshot(
            direction="outgoing",
            message_type="text",
            text="I remember blue",
            runtime_id="reply-1",
        ),
    ]
    config = WeChatDesktopConfig(
        log_path=tmp_path / "wechat.jsonl",
        dedup_path=tmp_path / "dedup.jsonl",
        contacts=WeChatDesktopConfig.contacts.__class__(
            default_policy="deny",
            allow_contact_keys=("alice-key",),
        ),
        monitor=WeChatDesktopConfig.monitor.__class__(buffer_seconds=0),
    )
    monitor = MessageMonitor(config=config, client=client)

    ready = monitor.poll_once()

    assert ready == []
    assert "ignored_already_replied_tail" in (tmp_path / "wechat.jsonl").read_text(encoding="utf-8")


def test_monitor_deduplicates_uia_message_when_runtime_order_changes(tmp_path):
    client = FakeWeChatDesktopClient()
    client.messages_by_contact["alice-key"] = [
        ChatMessageSnapshot(
            direction="incoming",
            message_type="text",
            text="same",
            runtime_id="runtime-1",
            relative_order=1,
            metadata={"fingerprint": "uia:alice:same"},
        )
    ]
    config = WeChatDesktopConfig(
        log_path=tmp_path / "wechat.jsonl",
        dedup_path=tmp_path / "dedup.jsonl",
        contacts=WeChatDesktopConfig.contacts.__class__(
            default_policy="deny",
            allow_contact_keys=("alice-key",),
        ),
        monitor=WeChatDesktopConfig.monitor.__class__(buffer_seconds=0),
    )
    monitor = MessageMonitor(config=config, client=client)

    first = monitor.poll_once()
    client.messages_by_contact["alice-key"] = [
        ChatMessageSnapshot(
            direction="incoming",
            message_type="text",
            text="same",
            runtime_id="runtime-2",
            relative_order=4,
            metadata={"fingerprint": "uia:alice:same"},
        )
    ]
    second = monitor.poll_once()

    assert len(first) == 1
    assert second == []


def test_buffer_merges_same_contact_and_keeps_fingerprints():
    buffer = MessageBuffer(buffer_seconds=1.5, max_text_chars=100)
    one = DesktopMessage(
        account_key="role",
        contact_key="alice-key",
        contact_display_name="Alice",
        chat_title="Alice",
        direction="incoming",
        message_type="text",
        text="one",
        runtime_id="m1",
    )
    two = DesktopMessage(
        account_key="role",
        contact_key="alice-key",
        contact_display_name="Alice",
        chat_title="Alice",
        direction="incoming",
        message_type="text",
        text="two",
        runtime_id="m2",
    )

    assert buffer.add(one, now=10.0) == []
    assert buffer.add(two, now=10.5) == []
    ready = buffer.flush_expired(now=12.0)

    assert len(ready) == 1
    assert ready[0].text == "one\ntwo"
    assert ready[0].fingerprints == (one.fingerprint, two.fingerprint)


def test_sender_uses_single_ui_path_and_verifies_tail():
    client = FakeWeChatDesktopClient()
    sender = WeChatDesktopSender(client=client, ui=WeChatDesktopConfig.ui)

    result = asyncio.run(sender.send_text("alice-key", "reply", "req-1"))

    assert result.ok is True
    assert result.verified is True
    assert client.sent == [("alice-key", "reply")]


def test_sender_keeps_stage12_pending_tasks_per_contact():
    client = FakeWeChatDesktopClient()
    sender = WeChatDesktopSender(client=client, ui=WeChatDesktopConfig.ui)

    first = asyncio.run(sender.send_text("n", "reply to n", "req-n"))
    second = asyncio.run(sender.send_text("L", "reply to L", "req-l"))

    assert first.ok is True
    assert second.ok is True
    assert client.sent == [("n", "reply to n"), ("L", "reply to L")]


def test_stage12_reply_uses_isolated_wechat_session_id(tmp_path):
    class FakeCore:
        def __init__(self) -> None:
            self.session_counts: dict[str, int] = {}
            self.messages = []

        def process(self, message):
            self.messages.append(message)
            count = self.session_counts.get(message.session_id, 0) + 1
            self.session_counts[message.session_id] = count
            return SimpleNamespace(
                ok=True,
                text=f"{message.session_id}:{count}",
                state="COMPLETED",
                error="",
            )

    class FakeSender:
        def __init__(self) -> None:
            self.sent = []

        async def send_text(self, contact_key, text, request_id):
            self.sent.append((contact_key, text, request_id))
            return SendResult(ok=True, request_id=request_id, contact_key=contact_key, verified=True)

    def batch(contact_key: str, text: str) -> BufferedDesktopBatch:
        return BufferedDesktopBatch(
            account_key="role_account_main",
            contact_key=contact_key,
            contact_display_name=contact_key,
            chat_title=contact_key,
            messages=(
                DesktopMessage(
                    account_key="role_account_main",
                    contact_key=contact_key,
                    contact_display_name=contact_key,
                    chat_title=contact_key,
                    direction="incoming",
                    message_type="text",
                    text=text,
                    runtime_id=f"{contact_key}-1",
                ),
            ),
        )

    core = FakeCore()
    sender = FakeSender()

    asyncio.run(
        _reply_to_batch(
            batch("n", "A says remember blue"),
            core=core,
            sender=sender,
            character_id="elysia",
            log_path=tmp_path / "wechat.jsonl",
        )
    )
    asyncio.run(
        _reply_to_batch(
            batch("L", "B asks a fresh question"),
            core=core,
            sender=sender,
            character_id="elysia",
            log_path=tmp_path / "wechat.jsonl",
        )
    )

    assert core.messages[0].session_id == "wechat_desktop:role_account_main:n"
    assert core.messages[1].session_id == "wechat_desktop:role_account_main:L"
    assert core.session_counts == {
        "wechat_desktop:role_account_main:n": 1,
        "wechat_desktop:role_account_main:L": 1,
    }
    assert sender.sent[0][0:2] == ("n", "wechat_desktop:role_account_main:n:1")
    assert sender.sent[1][0:2] == ("L", "wechat_desktop:role_account_main:L:1")


def test_deduplicator_persists_bounded_fingerprints(tmp_path):
    message = DesktopMessage(
        account_key="role",
        contact_key="alice-key",
        contact_display_name="Alice",
        chat_title="Alice",
        direction="incoming",
        message_type="text",
        text="hello",
        runtime_id="m1",
    )
    dedup = MessageDeduplicator(max_recent=10, state_path=tmp_path / "dedup.jsonl")

    assert dedup.seen(message) is False
    assert dedup.seen(message) is True
    restored = MessageDeduplicator(max_recent=10, state_path=tmp_path / "dedup.jsonl")
    assert restored.seen(message) is True


def test_health_allows_start_without_current_input_controls(monkeypatch):
    class HealthClient(FakeWeChatDesktopClient):
        def snapshot(self):
            return UISnapshot(
                logged_in=True,
                controls={
                    "chat_list": True,
                    "chat_title": True,
                    "message_list": True,
                    "input_box": False,
                    "send_button": False,
                },
            )

    monkeypatch.setattr(
        "runtime.adapters.wechat_desktop.health.find_wechat_processes",
        lambda _names: (SimpleNamespace(pid=1, name="WeChat.exe"),),
    )
    monkeypatch.setattr("runtime.adapters.wechat_desktop.health.is_desktop_unlocked", lambda: True)

    health = check_wechat_desktop_health(WeChatDesktopConfig(), HealthClient())

    assert health.ok is True


def test_process_locator_matches_windows_weixin_process_stem(monkeypatch):
    class FakeProcess:
        info = {"pid": 123, "name": "Weixin", "exe": r"D:\wechat\Weixin\Weixin.exe"}

    fake_psutil = SimpleNamespace(process_iter=lambda _attrs: [FakeProcess()], Error=Exception)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    processes = find_wechat_processes(("Weixin.exe", "WeChat.exe"))

    assert len(processes) == 1
    assert processes[0].pid == 123
    assert processes[0].name == "Weixin"


class FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def mid_point(self):
        return SimpleNamespace(x=(self.left + self.right) // 2, y=(self.top + self.bottom) // 2)


class FakeUIControl:
    def __init__(self, control_type: str, text: str, rect: FakeRect, *, class_name: str = "") -> None:
        self.element_info = SimpleNamespace(control_type=control_type, name=text)
        self._text = text
        self._rect = rect
        self._class_name = class_name

    def class_name(self):
        return self._class_name

    def window_text(self):
        return self._text

    def rectangle(self):
        return self._rect

    def descendants(self, **_kwargs):
        return ()


class FakeUIWindow(FakeUIControl):
    handle = 42

    def __init__(self, descendants: list[FakeUIControl]) -> None:
        super().__init__("Window", "微信", FakeRect(100, 100, 1000, 800), class_name="WeChatMainWndForPC")
        self._descendants = descendants

    def descendants(self, **_kwargs):
        return self._descendants

    def is_visible(self):
        return True


class FakeMessageList(FakeUIControl):
    def __init__(self, text: str | tuple[str, ...]) -> None:
        super().__init__("List", "messages", FakeRect(420, 200, 990, 600))
        texts = (text,) if isinstance(text, str) else text
        self._messages = tuple(
            FakeUIControl(
                "Text",
                value,
                FakeRect(450, 300 + index * 50, 650, 340 + index * 50),
                class_name="mmui::ChatTextItemView",
            )
            for index, value in enumerate(texts)
        )

    def children(self):
        return self._messages


class FakeMessageRow(FakeUIControl):
    def __init__(self, text: str, *, sender: str, sender_left: int, text_left: int) -> None:
        super().__init__("ListItem", text, FakeRect(420, 300, 990, 353))
        self._children = (
            FakeUIControl("Button", sender, FakeRect(sender_left, 306, sender_left + 34, 340)),
            FakeUIControl("Text", text, FakeRect(text_left, 313, text_left + 120, 333)),
        )

    def descendants(self, **_kwargs):
        return self._children


class FakeMessageRowList(FakeUIControl):
    def __init__(self, *rows: FakeMessageRow) -> None:
        super().__init__("List", "messages", FakeRect(420, 200, 990, 600))
        self._rows = rows

    def children(self):
        return self._rows


def test_pyweixin_snapshot_uses_visible_uia_window_when_legacy_running_check_fails(monkeypatch):
    class FakeTools:
        @staticmethod
        def is_weixin_running():
            return False

        @staticmethod
        def about_weixin():
            return {}

    controls = [
        FakeUIControl("List", "会话", FakeRect(150, 200, 400, 750)),
        FakeUIControl("Text", "n", FakeRect(450, 130, 470, 160)),
        FakeUIControl("List", "消息", FakeRect(420, 200, 990, 600)),
        FakeUIControl("Edit", "n", FakeRect(420, 650, 990, 700)),
        FakeUIControl("Button", "发送(S)", FakeRect(850, 710, 950, 745)),
    ]
    client = PyWeixinDesktopClient.__new__(PyWeixinDesktopClient)
    client.tools = FakeTools
    client.account_display_name = ""
    client._open_weixin = lambda is_maximize=False: FakeUIWindow(controls)
    monkeypatch.setattr(wechat_client, "_is_known_wechat_process_running", lambda: False)

    snapshot = client.snapshot()

    assert snapshot.logged_in is True
    assert all(snapshot.controls[name] for name in ("chat_list", "chat_title", "message_list", "input_box", "send_button"))


def test_current_chat_scrape_reacquires_message_list_each_poll(monkeypatch):
    state = {"text": "hello one"}

    def fake_open_window():
        return FakeUIWindow(
            [
                FakeUIControl("List", "sessions", FakeRect(150, 200, 400, 750)),
                FakeUIControl("Text", "n", FakeRect(450, 130, 470, 160)),
                FakeMessageList(state["text"]),
                FakeUIControl("Edit", "", FakeRect(420, 650, 990, 700)),
            ]
        )

    monkeypatch.setattr(wechat_client, "_open_weixin_by_visible_window", fake_open_window)

    first = wechat_client._scrape_current_chat_messages(contact_key="n", limit=5)
    state["text"] = "hello two"
    second = wechat_client._scrape_current_chat_messages(contact_key="n", limit=5)

    assert first[-1].text == "hello one"
    assert second[-1].text == "hello two"


def test_current_chat_scrape_distinguishes_repeated_visible_text(monkeypatch):
    def fake_open_window():
        return FakeUIWindow(
            [
                FakeUIControl("List", "sessions", FakeRect(150, 200, 400, 750)),
                FakeUIControl("Text", "n", FakeRect(450, 130, 470, 160)),
                FakeMessageList(("same", "same")),
                FakeUIControl("Edit", "", FakeRect(420, 650, 990, 700)),
            ]
        )

    monkeypatch.setattr(wechat_client, "_open_weixin_by_visible_window", fake_open_window)

    messages = wechat_client._scrape_current_chat_messages(contact_key="n", limit=5)
    fingerprints = [item.metadata["fingerprint"] for item in messages]

    assert [item.text for item in messages] == ["same", "same"]
    assert len(set(fingerprints)) == 2


def test_current_chat_scrape_uses_sender_child_for_full_width_rows(monkeypatch):
    def fake_open_window():
        return FakeUIWindow(
            [
                FakeUIControl("List", "sessions", FakeRect(150, 200, 400, 750)),
                FakeUIControl("Text", "n", FakeRect(450, 130, 470, 160)),
                FakeMessageRowList(
                    FakeMessageRow("reply", sender="elysia", sender_left=930, text_left=780),
                    FakeMessageRow("new text", sender="n", sender_left=450, text_left=505),
                ),
                FakeUIControl("Edit", "", FakeRect(420, 650, 990, 700)),
            ]
        )

    monkeypatch.setattr(wechat_client, "_open_weixin_by_visible_window", fake_open_window)

    messages = wechat_client._scrape_current_chat_messages(contact_key="n", own_name="elysia", limit=5)

    assert [item.direction for item in messages] == ["outgoing", "incoming"]
    assert messages[-1].text == "new text"


def test_pyweixin_client_adapts_new_message_dictionary():
    class FakeTools:
        @staticmethod
        def is_weixin_running():
            return True

        @staticmethod
        def about_weixin():
            return {"wxid": "role", "\u7248\u672c": "4.1.9.30"}

    class FakeNavigator:
        @staticmethod
        def open_weixin(is_maximize=False):
            return SimpleNamespace(handle=1, window_text=lambda: "Weixin", is_visible=lambda: True)

        @staticmethod
        def open_dialog_window(friend, is_maximize=False, search_pages=5):
            return SimpleNamespace(handle=2, window_text=lambda: friend, is_visible=lambda: True)

    class FakeMessages:
        @staticmethod
        def check_new_messages(is_maximize=False, search_pages=20, close_weixin=False):
            return {
                "n": [
                    {
                        "\u6d88\u606f\u53d1\u9001\u4eba": "n",
                        "\u6d88\u606f\u5185\u5bb9": "hello",
                        "\u6d88\u606f\u7c7b\u578b": "\u6587\u672c",
                    }
                ]
            }

    client = PyWeixinDesktopClient.__new__(PyWeixinDesktopClient)
    client.tools = FakeTools
    client.navigator = FakeNavigator
    client.messages = FakeMessages
    client.current_contact = ""
    client.account_display_name = "role"
    client._message_cache = {}
    client._use_current_chat_for_contact = set()

    sessions = client.list_sessions(limit=5)
    client.open_chat("n")
    messages = client.recent_messages(limit=5)

    assert sessions[0].contact_key == "n"
    assert messages[0].direction == "incoming"
    assert messages[0].message_type == "text"
    assert messages[0].text == "hello"


def test_pyweixin_client_falls_back_to_current_chat_when_open_dialog_fails(monkeypatch):
    class FakeNavigator:
        @staticmethod
        def open_dialog_window(friend, is_maximize=False, search_pages=5):
            raise RuntimeError("missing sidebar control")

    class FakeMessages:
        @staticmethod
        def pull_messages(**_kwargs):
            raise RuntimeError("missing sidebar control")

    fallback_messages = (
        ChatMessageSnapshot(
            direction="incoming",
            message_type="text",
            text="fallback hello",
            runtime_id="fallback-1",
        ),
    )
    monkeypatch.setattr(
        wechat_client,
        "_recent_messages_from_current_chat",
        lambda **_kwargs: fallback_messages,
    )

    client = PyWeixinDesktopClient.__new__(PyWeixinDesktopClient)
    client.navigator = FakeNavigator
    client.messages = FakeMessages
    client.current_contact = ""
    client.account_display_name = "role"
    client._message_cache = {}
    client._use_current_chat_for_contact = set()
    client.locate_main_window = lambda: WindowInfo(title="微信", handle="1", visible=True)

    client.open_chat("n")
    messages = client.recent_messages(limit=5)

    assert client.current_contact == "n"
    assert "n" in client._use_current_chat_for_contact
    assert messages == fallback_messages


def test_pyweixin_client_does_not_reuse_local_send_cache_for_next_poll(monkeypatch):
    scrape_calls = []

    def fake_recent_messages(**_kwargs):
        scrape_calls.append(1)
        return (
            ChatMessageSnapshot(
                direction="incoming",
                message_type="text",
                text=f"fresh-{len(scrape_calls)}",
                runtime_id=f"fresh-{len(scrape_calls)}",
                metadata={"source": "uia.current_chat_scrape"},
            ),
        )

    monkeypatch.setattr(wechat_client, "_recent_messages_from_current_chat", fake_recent_messages)

    client = PyWeixinDesktopClient.__new__(PyWeixinDesktopClient)
    client.current_contact = "n"
    client.account_display_name = "role"
    client._use_current_chat_for_contact = {"n"}
    client._message_cache = {
        "n": (
            ChatMessageSnapshot(
                direction="outgoing",
                message_type="text",
                text="cached reply",
                runtime_id="sent-1",
                metadata={"source": "local_send_cache"},
            ),
        )
    }

    messages = client.recent_messages(limit=5)

    assert len(scrape_calls) == 1
    assert messages[0].text == "fresh-1"


def test_pyweixin_client_can_verify_tail_from_local_send_cache():
    client = PyWeixinDesktopClient.__new__(PyWeixinDesktopClient)
    client.current_contact = "n"
    client._message_cache = {
        "n": (
            ChatMessageSnapshot(
                direction="outgoing",
                message_type="text",
                text="cached reply",
                runtime_id="sent-1",
                metadata={"source": "local_send_cache"},
            ),
        )
    }

    assert client.has_own_text_at_tail("cached reply") is True
