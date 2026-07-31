from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from runtime.adapters.wechat_desktop.client import PyWeixinDesktopClient
from runtime.adapters.wechat_desktop.call_contact_resolver import CallContactResolver
from runtime.adapters.wechat_desktop.call_controller import CallStateTracker, WeChatCallController
from runtime.adapters.wechat_desktop.call_detection_thread import ReadOnlyCallDetectionThread
from runtime.adapters.wechat_desktop.call_health import CallHealthChecker
from runtime.adapters.wechat_desktop.call_window_locator import WeChatCallWindowLocator
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig, load_wechat_desktop_config
from runtime.adapters.wechat_desktop.health import check_wechat_desktop_health
from runtime.adapters.wechat_desktop.incoming_call_watcher import IncomingCallWatcher
from runtime.adapters.wechat_desktop.message_monitor import MessageMonitor
from runtime.adapters.wechat_desktop.models import BufferedDesktopBatch, DesktopMessage, SendResult
from runtime.adapters.wechat_desktop.text_sender import WeChatDesktopSender
from runtime.adapters.wechat_desktop.ui_queue import SingleUIQueue
from runtime.adapters.wechat_desktop.voice_capture import WeChatVoiceCaptureService
from runtime.core.models import UnifiedMessage, new_request_id
from runtime.core.service import AlysiaCore
from runtime.gpt_sovits.types import SpeechRequest, SpeechResult
from runtime.qwen3.config import load_qwen3_config
from runtime.qwen3.service import Qwen3ChatSession
from runtime.scheduler.inference_queue import SingleGPUQueue
from runtime.scheduler.serial_queue import QueueEvent
from runtime.sensevoice.service import SenseVoiceRecognizer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 16 Windows WeChat desktop text, voice, and call adapter.")
    parser.add_argument("--character-id", "--character", default="elysia")
    parser.add_argument("--config", default="configs/runtime/wechat.yaml")
    parser.add_argument("--qwen3-config", default="configs/runtime/qwen3.yaml")
    parser.add_argument("--sensevoice-config", default="configs/runtime/sensevoice.yaml")
    parser.add_argument("--output-dir", default="storage/generated_audio/wechat_desktop_text")
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--enable-calls", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_wechat_desktop_config(args.config)
        client = PyWeixinDesktopClient(config.client.pywechat_root)
        if not args.skip_health:
            health = check_wechat_desktop_health(config, client)
            if not health.ok:
                for detail in health.details:
                    print(f"WeChat desktop health check failed: {detail}", file=sys.stderr)
                return 1
        asyncio.run(_run(args, config, client))
        return 0
    except KeyboardInterrupt:
        print("\nWindows WeChat desktop adapter stopped.")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


async def _run(args, config: WeChatDesktopConfig, client) -> None:
    core = AlysiaCore(
        transcriber=SenseVoiceRecognizer.from_config(args.sensevoice_config),
        chat_session_factory=lambda session_id: Qwen3ChatSession.from_config(
            _qwen3_config_for_character(args.qwen3_config, args.character_id)
        ),
        synthesizer=NullSynthesizer(),
        output_dir=args.output_dir,
        tts_character_id=args.character_id,
        synthesize_replies=False,
    )
    monitor = MessageMonitor(config=config, client=client)
    shared_ui_queue = SingleUIQueue(
        max_pending=config.scheduler.ui_max_pending,
        operation_timeout_seconds=config.ui.operation_timeout_seconds,
        pause_max_pending=config.scheduler.ui_pause_max_pending,
        event_sink=lambda event: _bridge_queue_event(config.log_path, event),
    )
    gpu_queue = SingleGPUQueue(
        max_pending=config.scheduler.gpu_max_pending,
        total_timeout_seconds=config.scheduler.total_message_seconds,
        cancel_pending_same_session=config.scheduler.cancel_pending_same_session,
        event_sink=lambda event: _bridge_queue_event(config.log_path, event),
    )
    sender = WeChatDesktopSender(client=client, ui=config.ui, queue=shared_ui_queue)
    voice_capture = WeChatVoiceCaptureService(
        client=client,
        voice=config.voice,
        ui=config.ui,
        queue=shared_ui_queue,
        executable_names=config.client.executable_names,
    )
    call_watcher = _build_call_watcher(config=config, client=client, queue=shared_ui_queue) if args.enable_calls else None
    call_detector = (
        ReadOnlyCallDetectionThread(
            config=config,
            interval_seconds=config.calls.poll_interval_seconds,
        )
        if call_watcher is not None
        else None
    )
    if call_detector is not None:
        call_detector.start()
    call_task = (
        asyncio.create_task(
            _run_call_watcher_loop(
                call_watcher,
                detector=call_detector,
                config=config,
                json_output=args.json,
            )
        )
        if call_watcher is not None and call_detector is not None
        else None
    )
    print("Stage 16 Windows WeChat adapter is polling. Keep the test WeChat logged in and visible.")
    try:
        while True:
            if call_watcher is not None and _should_pause_message_polling_for_calls(config, call_watcher):
                await asyncio.sleep(config.monitor.poll_interval_ms / 1000.0)
                continue
            async for batch in monitor.iter_ready_batches_async(
                should_stop=lambda: call_watcher is not None and _should_pause_message_polling_for_calls(
                    config,
                    call_watcher,
                )
            ):
                result = await _reply_to_batch(
                    batch,
                    core=core,
                    sender=sender,
                    voice_capture=voice_capture,
                    gpu_queue=gpu_queue,
                    character_id=args.character_id,
                    log_path=config.log_path,
                )
                _print(result.to_dict(), json_output=args.json)
                if args.once:
                    return
            await asyncio.sleep(config.monitor.poll_interval_ms / 1000.0)
    finally:
        if call_task is not None:
            call_task.cancel()
            with suppress(asyncio.CancelledError):
                await call_task
        if call_detector is not None:
            call_detector.stop()


def _build_call_watcher(*, config: WeChatDesktopConfig, client, queue: SingleUIQueue) -> IncomingCallWatcher:
    locator = WeChatCallWindowLocator(config=config, client=client)
    resolver = CallContactResolver(account_key=config.account.account_key, client=client)
    state = CallStateTracker()
    controller = WeChatCallController(
        locator=locator,
        resolver=resolver,
        ui=config.ui,
        queue=queue,
        state=state,
    )
    health_checker = CallHealthChecker(config=config)
    return IncomingCallWatcher(
        config=config,
        locator=locator,
        resolver=resolver,
        controller=controller,
        health_checker=health_checker,
        state=state,
        event_sink=lambda event, fields: _bridge_event(config.log_path, event, **fields),
    )


def _should_pause_message_polling_for_calls(
    config: WeChatDesktopConfig,
    call_watcher: IncomingCallWatcher,
) -> bool:
    return config.calls.pause_message_polling_while_enabled and call_watcher.state.ordinary_messages_paused


async def _run_call_watcher_loop(
    call_watcher: IncomingCallWatcher,
    *,
    detector: ReadOnlyCallDetectionThread,
    config: WeChatDesktopConfig,
    json_output: bool,
) -> None:
    last_sequence = 0
    while True:
        try:
            frame = detector.latest()
            if frame.sequence != last_sequence:
                last_sequence = frame.sequence
                if frame.error:
                    _bridge_event(config.log_path, "call_detection_failed", error=frame.error, retryable=True)
                results = await call_watcher.handle_detected_calls(frame.calls)
                for call_result in results:
                    _print(call_result.to_dict(), json_output=json_output)
                _update_call_detector_pause(detector, results)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _bridge_event(config.log_path, "call_watcher_failed", error=str(exc), retryable=True)
        await asyncio.sleep(0.05)


def _update_call_detector_pause(detector: ReadOnlyCallDetectionThread, results) -> None:
    if not results:
        detector.resume()
        return
    actions = {result.action for result in results}
    if "answer" in actions or "reject" in actions or "hangup" in actions:
        detector.pause(seconds=5.0)
        return
    detector.pause(seconds=1.0)


async def _reply_to_batch(
    batch: BufferedDesktopBatch,
    *,
    core: AlysiaCore,
    sender: WeChatDesktopSender,
    character_id: str,
    log_path: Path,
    voice_capture: WeChatVoiceCaptureService | None = None,
    gpu_queue: SingleGPUQueue | None = None,
) -> SendResult:
    request_id = new_request_id()
    voice_message = _single_voice_message(batch)
    _bridge_event(
        log_path,
        "voice_batch_ready" if voice_message else "batch_ready",
        request_id=request_id,
        contact_key=batch.contact_key,
        text_chars=len(batch.text),
        message_count=len(batch.messages),
    )
    print(
        f"Received WeChat {'voice' if voice_message else 'text'} batch from {batch.contact_key}.",
        flush=True,
    )
    try:
        _bridge_event(log_path, "reply_generation_started", request_id=request_id, contact_key=batch.contact_key)
        if voice_message is not None:
            if voice_capture is None:
                return SendResult(
                    ok=False,
                    request_id=request_id,
                    contact_key=batch.contact_key,
                    error="voice capture service is not configured",
                )
            print("Capturing WeChat voice bubble and normalizing WAV...", flush=True)
            capture = await voice_capture.capture(voice_message, request_id)
            _bridge_event(
                log_path,
                "voice_capture_result",
                request_id=request_id,
                contact_key=batch.contact_key,
                ok=capture.ok,
                error=capture.error,
                capture_method=capture.capture_method,
                normalize_method=capture.normalize_method,
            )
            if not capture.ok:
                return SendResult(
                    ok=False,
                    request_id=request_id,
                    contact_key=batch.contact_key,
                    error=capture.error,
                    retryable=capture.retryable,
                )
            print("Transcribing with SenseVoice and generating reply with Qwen3...", flush=True)
            core_message = capture.to_message(voice_message, character_id=character_id)
        else:
            print("Generating reply with Qwen3...", flush=True)
            core_message = UnifiedMessage(
                request_id=request_id,
                channel="wechat_desktop",
                session_id=batch.session_id,
                sender_id=batch.contact_key,
                sender_name=batch.contact_display_name,
                message_type="text",
                text=batch.text,
                character_id=character_id,
                metadata={
                    "chat_title": batch.chat_title,
                    "message_fingerprints": list(batch.fingerprints),
                },
            )
        if gpu_queue is None:
            reply = core.process(core_message)
        else:
            reply = await gpu_queue.run_inference(
                core_message,
                lambda: core.process(core_message),
                priority=30 if voice_message is not None else 50,
            )
    except Exception as exc:
        _bridge_event(
            log_path,
            "reply_generation_failed",
            request_id=request_id,
            contact_key=batch.contact_key,
            error=str(exc),
        )
        raise

    if not reply.text.strip():
        _bridge_event(
            log_path,
            "reply_empty",
            request_id=request_id,
            contact_key=batch.contact_key,
            error=reply.error,
        )
        return SendResult(
            ok=False,
            request_id=request_id,
            contact_key=batch.contact_key,
            error=reply.error or "AlysiaCore returned an empty text reply",
        )

    _bridge_event(
        log_path,
        "reply_ready",
        request_id=request_id,
        contact_key=batch.contact_key,
        reply_chars=len(reply.text),
        core_ok=reply.ok,
        core_state=reply.state,
        core_error=reply.error,
    )
    print(f"Reply ready ({len(reply.text)} chars). Sending to WeChat...", flush=True)
    result = await sender.send_text(batch.contact_key, reply.text, request_id)
    _bridge_event(
        log_path,
        "send_result",
        request_id=request_id,
        contact_key=batch.contact_key,
        ok=result.ok,
        error=result.error,
        attempts=result.attempts,
        verified=result.verified,
    )
    print(f"WeChat send result: ok={result.ok}, verified={result.verified}, error={result.error}", flush=True)
    return result


def _single_voice_message(batch: BufferedDesktopBatch) -> DesktopMessage | None:
    if len(batch.messages) == 1 and batch.messages[0].message_type == "voice":
        return batch.messages[0]
    return None


class NullSynthesizer:
    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        raise RuntimeError("Stage 11 desktop text adapter sends text replies only")


def _qwen3_config_for_character(config_path: str | Path, character_id: str):
    config = load_qwen3_config(config_path)
    character_root = Path("configs") / "characters" / character_id
    return replace(
        config,
        character_config=character_root / "character.yaml",
        system_prompt=character_root / "system_prompt.txt",
        examples_path=character_root / "examples.jsonl",
    )


def _print(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    else:
        print(payload, flush=True)


def _bridge_event(log_path: Path, event: str, **fields: object) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": int(time.time()), "event": event, **fields}
    with log_path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _bridge_queue_event(log_path: Path, event: QueueEvent) -> None:
    _bridge_event(log_path, "queue_" + event.event, **event.to_dict())


if __name__ == "__main__":
    raise SystemExit(main())
