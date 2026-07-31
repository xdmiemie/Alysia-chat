from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace

from runtime.adapters.wechat_desktop.call_models import CallButton, CallWindowSnapshot
from runtime.adapters.wechat_desktop.call_window_locator import WeChatCallWindowLocator
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig


@dataclass(frozen=True)
class CallDetectionFrame:
    sequence: int
    calls: tuple[CallWindowSnapshot, ...]
    error: str = ""
    observed_at: float = 0.0


class ReadOnlyCallDetectionThread:
    def __init__(
        self,
        *,
        config: WeChatDesktopConfig,
        interval_seconds: float,
        locator: WeChatCallWindowLocator | None = None,
    ) -> None:
        self.config = config
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.locator = locator or WeChatCallWindowLocator(config=config, client=None)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pause_until = 0.0
        self._latest = CallDetectionFrame(sequence=0, calls=(), observed_at=time.time())

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wechat-call-detector", daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 2.0) -> None:
        self._stop.set()
        self._paused.clear()
        if self._thread is not None:
            self._thread.join(timeout=timeout_seconds)

    def pause(self, seconds: float | None = None) -> None:
        with self._lock:
            self._pause_until = time.time() + max(0.0, seconds) if seconds is not None else 0.0
        self._paused.set()

    def resume(self) -> None:
        with self._lock:
            self._pause_until = 0.0
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def latest(self) -> CallDetectionFrame:
        with self._lock:
            return self._latest

    def _run(self) -> None:
        coinitialized = _coinitialize()
        try:
            while not self._stop.is_set():
                if self._is_paused():
                    self._stop.wait(0.05)
                    continue
                self._detect_once()
                self._stop.wait(self.interval_seconds)
        finally:
            if coinitialized:
                _couninitialize()

    def _is_paused(self) -> bool:
        if not self._paused.is_set():
            return False
        with self._lock:
            pause_until = self._pause_until
        if pause_until and time.time() >= pause_until:
            self.resume()
            return False
        return True

    def _detect_once(self) -> None:
        error = ""
        started = time.perf_counter()
        _terminal_log("开始检测通话窗口")
        try:
            calls = tuple(_read_only_snapshot(call) for call in self.locator.locate_incoming_calls())
        except Exception as exc:
            calls = ()
            error = str(exc)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if calls:
            _terminal_log(
                "检测到通话窗口",
                count=len(calls),
                contacts=",".join(call.contact_display_text or call.title or call.call_id for call in calls),
            )
        _terminal_log("结束检测通话窗口", count=len(calls), elapsed_ms=elapsed_ms, error=error)
        with self._lock:
            self._latest = CallDetectionFrame(
                sequence=self._latest.sequence + 1,
                calls=calls,
                error=error,
                observed_at=time.time(),
            )
        if calls:
            self.pause()


def _read_only_snapshot(call: CallWindowSnapshot) -> CallWindowSnapshot:
    return replace(
        call,
        buttons={role: _read_only_button(button) for role, button in call.buttons.items()},
    )


def _read_only_button(button: CallButton) -> CallButton:
    return replace(button, invoke=None, raw_control=None)


def _coinitialize() -> bool:
    try:
        import pythoncom

        pythoncom.CoInitialize()
        return True
    except Exception:
        return False


def _couninitialize() -> None:
    try:
        import pythoncom

        pythoncom.CoUninitialize()
    except Exception:
        return


def _terminal_log(message: str, **fields: object) -> None:
    suffix = " ".join(f"{key}={value}" for key, value in fields.items() if value not in (None, ""))
    print(f"[wechat] {message}{' ' + suffix if suffix else ''}", flush=True)
