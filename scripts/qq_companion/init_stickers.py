"""Create a small original, redistributable geometric sticker library."""

from pathlib import Path


def initialize(directory: Path):
    from PIL import Image, ImageDraw
    directory.mkdir(parents=True, exist_ok=True)
    for tag in ("happy", "sad", "surprised", "gentle", "serious"):
        path = directory / (tag + ".png")
        if path.exists():
            continue
        image = Image.new("RGBA", (256, 256), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((15, 15, 241, 241), fill="#ffe5a0", outline="#604739", width=6)
        for x in (82, 174):
            draw.ellipse((x - 8, 84, x + 8, 106), fill="#604739")
        if tag in {"happy", "gentle"}:
            draw.arc((74, 98, 182, 180), 5, 175, fill="#604739", width=7)
        elif tag == "sad":
            draw.arc((82, 157, 174, 200), 190, 350, fill="#604739", width=6)
            draw.ellipse((64, 116, 80, 149), fill="#78c8ef")
        elif tag == "surprised":
            draw.ellipse((108, 138, 148, 185), fill="#604739")
        else:
            draw.line((89, 163, 167, 163), fill="#604739", width=7)
        image.save(path)
    (directory / "LICENSE.txt").write_text(
        "Original geometric artwork created for astrbot-chat. CC0-1.0.\n", encoding="utf-8")


if __name__ == "__main__":
    initialize(Path(__file__).resolve().parents[2] / "storage/qq_companion/stickers")
