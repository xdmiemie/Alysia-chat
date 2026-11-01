"""Generate original, reproducible media with known pixel/time ground truth."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont

from integrations.qq_astrbot.plugin.vision_media import ffmpeg_tool

COLORS = [("red", "红色"), ("blue", "蓝色"), ("green", "绿色"), ("yellow", "黄色"), ("purple", "紫色")]


def font(size=32):
    return ImageFont.truetype("C:/Windows/Fonts/arial.ttf", size)


def generate(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "fixtures.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = {"image": [], "sticker": [], "gif": [], "video": []}
    for index in range(20):
        color, label = COLORS[index % len(COLORS)]
        shape = index // 5
        image = Image.new("RGB", (640, 480), "white")
        draw = ImageDraw.Draw(image)
        count = index % 3 + 1
        for i in range(count):
            x = 30 + i * 205
            box = (x, 100, x + 150, 250)
            if shape % 2:
                draw.ellipse(box, fill=color)
            else:
                draw.rectangle(box, fill=color)
        draw.text((30, 320), f"CARD {index + 1:02d}", fill="black", font=font())
        path = directory / f"image-{index:02d}.png"
        image.save(path)
        rows["image"].append({"path": str(path), "expected": label, "count": count,
            "question": "图中主要几何图形是什么颜色？有几个？请说明可见文字。"})
        mood = ("HAPPY", "SAD", "ANGRY", "WOW")[index % 4]
        face = Image.new("RGB", (400, 400), "white")
        d = ImageDraw.Draw(face)
        d.ellipse((60, 30, 340, 310), fill=(255, 220 - index * 2, 90), outline="black", width=4)
        d.ellipse((135, 105, 155, 130), fill="black")
        d.ellipse((245, 105, 265, 130), fill="black")
        if mood == "HAPPY":
            d.arc((140, 150, 260, 250), 0, 180, fill="black", width=8)
        elif mood == "SAD":
            d.arc((140, 200, 260, 270), 180, 360, fill="black", width=8)
        elif mood == "ANGRY":
            d.line((125, 85, 165, 105), fill="black", width=8)
            d.line((235, 105, 275, 85), fill="black", width=8)
            d.line((150, 220, 250, 220), fill="black", width=8)
        else:
            d.ellipse((170, 180, 230, 255), fill="black")
        d.text((80, 335), mood, font=font(36), fill="black")
        path = directory / f"sticker-{index:02d}.png"
        face.save(path)
        rows["sticker"].append({"path": str(path), "expected": mood,
            "question": "这张表情包是什么情绪？请读出可见的英文文字。"})
    for index in range(10):
        colors = [COLORS[(index + j) % 5] for j in range(4)]
        frames = []
        for j, (color, _) in enumerate(colors):
            image = Image.new("RGB", (320, 240), "white")
            d = ImageDraw.Draw(image)
            d.rectangle((20 + j * 45, 50, 120 + j * 45, 150), fill=color)
            frames.append(image)
        path = directory / f"gif-{index:02d}.gif"
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=250 + index * 20, loop=0)
        rows["gif"].append({"path": str(path), "expected": [c[1] for c in colors],
            "question": "按先后顺序描述图形的颜色和位置变化。"})
    for index in range(20):
        duration = (12, 24, 48, 60)[index] if index < 4 else 6 + index % 3 * 2
        first, last = COLORS[index % 5], COLORS[(index + 1) % 5]
        path = directory / f"video-{index:02d}.mp4"
        args = [ffmpeg_tool("ffmpeg"), "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
            f"color=c={first[0]}:s=320x240:r=5:d={duration / 2}", "-f", "lavfi", "-i",
            f"color=c={last[0]}:s=320x240:r=5:d={duration / 2}"]
        if index % 2:
            args += ["-f", "lavfi", "-i", f"sine=frequency={440 + index * 30}:sample_rate=16000:duration={duration}"]
        filter_graph = (f"[0:v]drawtext=text='{first[0].upper()} {index}':fontsize=26:fontcolor=white:x=20:y=30[a];"
            f"[1:v]drawtext=text='{last[0].upper()} {index}':fontsize=26:fontcolor=white:x='mod(t*{50 + index * 10},220)':y=100[b];"
            "[a][b]concat=n=2:v=1:a=0[v]")
        args += ["-filter_complex", filter_graph, "-map", "[v]"]
        if index % 2:
            args += ["-map", "2:a", "-c:a", "aac"]
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-threads", "2", "-pix_fmt", "yuv420p", str(path)]
        subprocess.run(args, capture_output=True, timeout=60, check=True)
        rows["video"].append({"path": str(path), "expected": [first[1], last[1]], "duration": duration,
            "has_audio": bool(index % 2), "question": "按时间顺序描述画面主要颜色、文字及变化。"})
    manifest_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return rows
