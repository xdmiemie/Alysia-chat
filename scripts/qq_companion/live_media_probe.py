"""Send small labeled Q1 media probes to an explicitly authorized QQ test peer."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import struct
import urllib.request
import wave
import zlib
from io import BytesIO
from configure import locate_snowluma_config, read_json


def png_card() -> bytes:
    width, height = 160, 96
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            light = ((x // 16) + (y // 16)) % 2 == 0
            rows.extend((40, 130 if light else 70, 220 if light else 145))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def wave_tone() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        samples = [int(5000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(16000)]
        wav.writeframes(struct.pack("<" + "h" * len(samples), *samples))
    return output.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="QQ friend explicitly authorized for this test")
    parser.add_argument("--target", choices=("private", "group", "both"), default="private")
    parser.add_argument("--kind", choices=("image", "voice", "both"), default="both")
    args = parser.parse_args()
    config = read_json(locate_snowluma_config(os.getenv("QQ_COMPANION_SNOWLUMA_CONFIG")))
    servers = [s for s in config.get("networks", {}).get("httpServers", []) if s.get("port") == 3000]
    if len(servers) != 1 or servers[0].get("host") not in ("127.0.0.1", "localhost"):
        raise SystemExit("Local SnowLuma OneBot HTTP server is not configured")
    token = servers[0].get("accessToken", "")

    def call(action: str, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"http://127.0.0.1:3000/{action}", data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    friends = call("get_friend_list", {})
    if friends.get("retcode") != 0 or args.user_id not in {str(item.get("user_id")) for item in friends.get("data", [])}:
        raise SystemExit("Target is not in the active QQ friend list")
    targets = []
    if args.target in ("private", "both"):
        targets.append(("private", "send_private_msg", {"user_id": int(args.user_id)}))
    if args.target in ("group", "both"):
        groups = call("get_group_list", {})
        if groups.get("retcode") != 0 or len(groups.get("data", [])) != 1:
            raise SystemExit("Expected one visible test group")
        targets.append(("group", "send_group_msg", {"group_id": groups["data"][0]["group_id"]}))

    media = []
    if args.kind in ("image", "both"):
        media.append(("image", png_card()))
    if args.kind in ("voice", "both"):
        media.append(("record", wave_tone()))
    success = True
    for target_name, action, target in targets:
        for kind, content in media:
            encoded = base64.b64encode(content).decode("ascii")
            message = [{"type": "text", "data": {"text": "【Q1测试】媒体收发验证"}},
                       {"type": kind, "data": {"file": f"base64://{encoded}"}}]
            result = call(action, {**target, "message": message})
            ok = result.get("retcode") == 0
            print(f"{target_name}_{kind}: {'accepted' if ok else 'failed'}")
            success &= ok
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
