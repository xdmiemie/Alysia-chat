from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from runtime.adapters.wechat_desktop.client import PyWeixinDesktopClient
from runtime.adapters.wechat_desktop.config import WeChatDesktopConfig, load_wechat_desktop_config
from runtime.adapters.wechat_desktop.health import check_wechat_desktop_health
from runtime.adapters.wechat_desktop.message_monitor import MessageMonitor
from runtime.adapters.wechat_desktop.models import BufferedDesktopBatch, SendResult
from runtime.adapters.wechat_desktop.text_sender import WeChatDesktopSender
from runtime.core.models import UnifiedMessage, new_request_id
from runtime.core.service import AlysiaCore
from runtime.gpt_sovits.types import SpeechRequest, SpeechResult
from runtime.qwen3.config import load_qwen3_config
from runtime.qwen3.service import Qwen3ChatSession
from runtime.sensevoice.types import TranscriptionResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 11 Windows WeChat desktop text adapter.")
    parser.add_argument("--character-id", "--character", default="elysia")
    parser.add_argument("--config", default="configs/runtime/wechat.yaml")
    parser.add_argument("--qwen3-config", default="configs/runtime/qwen3.yaml")
    parser.add_argument("--output-dir", default="storage/generated_audio/wechat_desktop_text")
    parser.add_argument("--skip-health", action="store_true")
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
        transcriber=NullTranscriber(),
        chat_session_factory=lambda session_id: Qwen3ChatSession.from_config(
            _qwen3_config_for_character(args.qwen3_config, args.character_id)
        ),
        synthesizer=NullSynthesizer(),
        output_dir=args.output_dir,
        tts_character_id=args.character_id,
        synthesize_replies=False,
    )
    monitor = MessageMonitor(config=config, client=client)
    sender = WeChatDesktopSender(client=client, ui=config.ui)
    print("Stage 11 Windows WeChat adapter is polling. Keep the test WeChat logged in and visible.")
    while True:
        for batch in monitor.iter_ready_batches():
            result = await _reply_to_batch(
                batch,
                core=core,
                sender=sender,
                character_id=args.character_id,
                log_path=config.log_path,
            )
            _print(result.to_dict(), json_output=args.json)
            if args.once:
                return
        await asyncio.sleep(config.monitor.poll_interval_ms / 1000.0)


async def _reply_to_batch(
    batch: BufferedDesktopBatch,
    *,
    core: AlysiaCore,
    sender: WeChatDesktopSender,
    character_id: str,
    log_path: Path,
) -> SendResult:
    request_id = new_request_id()
    _bridge_event(
        log_path,
        "batch_ready",
        request_id=request_id,
        contact_key=batch.contact_key,
        text_chars=len(batch.text),
        message_count=len(batch.messages),
    )
    print(f"Received WeChat text batch from {batch.contact_key}: {len(batch.text)} chars", flush=True)
    try:
        _bridge_event(log_path, "reply_generation_started", request_id=request_id, contact_key=batch.contact_key)
        print("Generating reply with Qwen3...", flush=True)
        reply = core.process(
            UnifiedMessage(
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


class NullTranscriber:
    def transcribe(self, audio_path: str | Path) -> TranscriptionResult:
        raise RuntimeError("Stage 11 desktop text adapter does not accept audio messages")


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


if __name__ == "__main__":
    raise SystemExit(main())
