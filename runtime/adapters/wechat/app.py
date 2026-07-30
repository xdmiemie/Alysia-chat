from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from runtime.adapters.wechat.config import load_wechat_config
from runtime.adapters.wechat.health import check_wechat_health
from runtime.adapters.wechat.ilink.api import ILinkApiClient
from runtime.adapters.wechat.ilink.credentials import load_credentials
from runtime.adapters.wechat.ilink.cursor_store import CursorStore
from runtime.adapters.wechat.ilink.event_parser import ILinkEventParser
from runtime.adapters.wechat.ilink.long_poller import ILinkLongPoller, LoginExpiredError
from runtime.adapters.wechat.ilink.sender import ILinkSender
from runtime.adapters.wechat.router import WeChatTextRouter
from runtime.core.service import AlysiaCore
from runtime.qwen3.config import load_qwen3_config
from runtime.qwen3.service import Qwen3ChatSession
from runtime.gpt_sovits.types import SpeechRequest, SpeechResult
from runtime.sensevoice.types import TranscriptionResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 11 Tencent iLink WeChat text adapter.")
    parser.add_argument("--provider", default="ilink")
    parser.add_argument("--character-id", "--character", default="elysia")
    parser.add_argument("--config", default="configs/runtime/wechat.yaml")
    parser.add_argument("--sensevoice-config", default="configs/runtime/sensevoice.yaml")
    parser.add_argument("--qwen3-config", default="configs/runtime/qwen3.yaml")
    parser.add_argument("--gpt-sovits-config", default="configs/runtime/gpt_sovits.yaml")
    parser.add_argument("--output-dir", default="storage/generated_audio/wechat_text")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--once", action="store_true", help="Process one non-empty long-poll response and exit.")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_wechat_config(args.config)
        if args.provider != "ilink" or config.provider != "ilink":
            print("Error: Stage 11 supports only --provider ilink.", file=sys.stderr)
            return 2
        if args.health:
            status = check_wechat_health(config)
            _print({"state": status.state, "detail": status.detail}, json_output=args.json)
            return 0 if status.ok else 1
        asyncio.run(_run(args, config))
        return 0
    except KeyboardInterrupt:
        print("\nWeChat iLink adapter stopped.")
        return 0
    except LoginExpiredError as exc:
        print(f"iLink login expired: {exc}. Run scripts/ilink_login.ps1 again.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


async def _run(args, config) -> None:
    credentials = load_credentials(config.ilink.credentials_path)
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
    api = ILinkApiClient(
        credentials=credentials,
        bot_agent=config.ilink.bot_agent,
        timeout_seconds=config.ilink.request_timeout_seconds + config.ilink.long_poll_extra_timeout_seconds,
        channel_version=config.ilink.channel_version,
    )
    router = WeChatTextRouter(
        config=config,
        core=core,
        sender=ILinkSender(api, max_text_length=config.reply.max_text_length),
    )
    poller = ILinkLongPoller(
        api=api,
        cursor_store=CursorStore(config.ilink.cursor_path),
        parser=ILinkEventParser(account_id=credentials.account_id, bot_user_id=credentials.bot_user_id),
        reconnect_seconds=config.ilink.reconnect_seconds,
        max_reconnect_seconds=config.ilink.max_reconnect_seconds,
    )

    print("Stage 11 iLink bridge is polling. Send a whitelisted WeChat text message to test it.")
    queue: asyncio.Queue = asyncio.Queue()
    producer = asyncio.create_task(_poll_to_queue(poller, queue, once=args.once))
    try:
        await _consume_queue(queue, router, json_output=args.json, once=args.once)
    finally:
        poller.stop()
        producer.cancel()
        for result in await router.flush():
            _print(result.to_dict(), json_output=args.json)


async def _poll_to_queue(poller: ILinkLongPoller, queue: asyncio.Queue, *, once: bool) -> None:
    async for messages in poller.messages():
        for message in messages:
            await queue.put(message)
        if once and messages:
            break
    await queue.put(None)


async def _consume_queue(
    queue: asyncio.Queue,
    router: WeChatTextRouter,
    *,
    json_output: bool,
    once: bool,
) -> None:
    sent_count = 0
    while True:
        timeout = router.next_flush_seconds()
        try:
            message = await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            for result in await router.flush_expired():
                sent_count += 1
                _print(result.to_dict(), json_output=json_output)
            if once and sent_count > 0:
                return
            continue

        if message is None:
            return

        result = await router.accept(message)
        if result is not None:
            sent_count += 1
            _print(result.to_dict(), json_output=json_output)
        for result in await router.flush_expired():
            sent_count += 1
            _print(result.to_dict(), json_output=json_output)
        if once and sent_count > 0:
            return


def _qwen3_config_for_character(config_path: str | Path, character_id: str):
    config = load_qwen3_config(config_path)
    character_root = Path("configs") / "characters" / character_id
    return replace(
        config,
        character_config=character_root / "character.yaml",
        system_prompt=character_root / "system_prompt.txt",
        examples_path=character_root / "examples.jsonl",
    )


class NullTranscriber:
    def transcribe(self, audio_path: str | Path) -> TranscriptionResult:
        raise RuntimeError("Stage 11 text adapter does not accept audio messages")


class NullSynthesizer:
    def synthesize(self, request: SpeechRequest) -> SpeechResult:
        raise RuntimeError("Stage 11 text adapter sends text replies only")


def _print(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(payload)


if __name__ == "__main__":
    raise SystemExit(main())
