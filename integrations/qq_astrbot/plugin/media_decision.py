"""Validate the same Agent's media decisions; never classify chat by keywords."""

from dataclasses import dataclass
import json


@dataclass(frozen=True)
class MediaDecision:
    voice: bool = False
    emotion: str = "neutral"
    music: dict | None = None
    sticker: str | None = None


def parse_song_reply(raw: str) -> str | None:
    """Keep the model's full short reply, including an ellipsis before a question."""
    try:
        value = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(value, dict):
        return None
    text, level = value.get("follow_up"), value.get("thinking_level")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 80 or type(level) is not int or not 1 <= level <= 5:
        return None
    return text.strip()


def parse_song_selection(raw: str, candidates: list[dict]) -> tuple[bool, int | None]:
    """Validate membership only; all title/version interpretation belongs to AI."""
    try:
        value = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except (ValueError, TypeError, AttributeError):
        return False, None
    if not isinstance(value, dict) or "song_id" not in value:
        return False, None
    song_id = value["song_id"]
    if song_id is None:
        return True, None
    return (True, song_id) if type(song_id) is int and any(s["song_id"] == song_id for s in candidates) else (False, None)


def parse_media(raw: str, *, voice_input: bool = False) -> MediaDecision:
    try:
        value = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except (ValueError, TypeError):
        return MediaDecision()
    if not isinstance(value, dict):
        return MediaDecision()
    # Legacy replies retain the documented input-type default. Explicit model
    # decisions implement editable persona rules, regardless of input modality.
    voice = value.get("voice", voice_input)
    if type(voice) is not bool:
        voice = False
    emotion = value.get("emotion", "neutral")
    if emotion not in {"neutral", "happy", "sad", "surprised", "gentle", "serious"}:
        emotion = "neutral"
    music = value.get("music")
    if (not isinstance(music, dict) or music.get("mode") not in {"original", "cover"} or
            not isinstance(music.get("query"), str) or not 1 <= len(music["query"].strip()) <= 120 or
            not isinstance(music.get("artist", ""), str) or len(music.get("artist", "")) > 100):
        music = None
    else:
        music = {"mode": music["mode"], "query": music["query"].strip(), "artist": music.get("artist", "").strip()}
    sticker = value.get("sticker")
    if sticker not in {"happy", "sad", "surprised", "gentle", "serious", None}:
        sticker = None
    return MediaDecision(voice, emotion, music, sticker)
