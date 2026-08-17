"""Local character resources used by the Stage 15 desktop editor.

The service deliberately owns files below ``configs/characters`` only.  Model
registries are read for validation but are never copied or deleted: base models
can be shared by many characters and must not disappear with a character.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import wave
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from runtime.audio.audio_format import WaveMetrics, inspect_wav, normalize_wav
from runtime.common.config import load_flat_yaml


DEFAULT_EMOTIONS = ("neutral", "happy", "gentle", "sad", "surprised", "serious")
_VALID_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

DEFAULT_PROMPT = """你正在扮演【{name}】。

角色设定：
- 在这里描述角色的身份、性格、价值观和与用户的关系。

说话方式：
- 使用自然、适合直接朗读的中文。
- 保持角色稳定，不随意改变人设。
- 根据上下文自然回应，不复读用户。

限制：
- 不输出内部思考过程。
- 不声称自己切换成其他角色。
"""


@dataclass
class ReferenceVoice:
    emotion: str
    text: str = ""
    filename: str = ""
    metrics: WaveMetrics | None = None


@dataclass
class CharacterDraft:
    character_id: str = "new_character"
    display_name: str = "新角色"
    description: str = ""
    language: str = "zh"
    reply_mode: str = "auto"
    history_turns: int = 8
    memory_enabled: bool = False
    qwen_model: str = ""
    system_prompt: str = ""
    personality: str = ""
    speaking_style: str = ""
    forbidden_behavior: str = ""
    temperature: float = 0.8
    top_p: float = 0.9
    max_tokens: int = 512
    tts_model: str = ""
    default_emotion: str = "neutral"
    speed: float = 1.0
    text_language: str = "zh"
    avatar_source: str = ""
    references: dict[str, ReferenceVoice] = field(default_factory=dict)

    @classmethod
    def template(cls, character_id: str = "new_character", name: str = "新角色") -> "CharacterDraft":
        return cls(
            character_id=character_id,
            display_name=name,
            system_prompt=DEFAULT_PROMPT.format(name=name),
            references={emotion: ReferenceVoice(emotion) for emotion in DEFAULT_EMOTIONS},
        )


@dataclass(frozen=True)
class CharacterValidation:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.errors

    @property
    def complete(self) -> bool:
        return not self.errors and not self.warnings


class CharacterManager:
    """Create, validate, import, export, and remove local character bundles."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.characters_dir = self.project_root / "configs" / "characters"
        self.registry_dir = self.project_root / "model_store" / "registry"

    def list_profiles(self) -> list[tuple[str, str]]:
        profiles: list[tuple[str, str]] = []
        for path in sorted(self.characters_dir.glob("*/character.yaml")):
            raw = load_flat_yaml(path)
            profiles.append((path.parent.name, str(raw.get("name") or raw.get("display_name") or path.parent.name)))
        return profiles

    def load(self, character_id: str) -> CharacterDraft:
        root = self._root_for(character_id)
        raw = load_flat_yaml(root / "character.yaml")
        prompt_path = root / "system_prompt.md"
        if not prompt_path.exists():
            prompt_path = root / "system_prompt.txt"
        references = self._read_references(root)
        draft = CharacterDraft.template(character_id, str(raw.get("name") or raw.get("display_name") or character_id))
        draft.description = str(raw.get("description") or "")
        draft.language = str(raw.get("language") or "zh")
        draft.reply_mode = str(raw.get("default_reply_mode") or "auto")
        draft.history_turns = _number(raw.get("history_turns"), 8, int)
        draft.memory_enabled = bool(raw.get("memory_enabled", False))
        # Legacy character folders predate Stage 15 and do not carry default
        # model IDs in character.yaml.  Their per-character registries remain
        # the source of truth, so surface the active model in the editor.
        draft.qwen_model = str(raw.get("qwen3_default_model") or self._active_model_id("qwen3", character_id))
        draft.tts_model = str(raw.get("gpt_sovits_default_model") or self._active_model_id("gpt_sovits", character_id))
        draft.default_emotion = str(raw.get("default_emotion") or "neutral")
        draft.speed = _number(raw.get("speed"), 1.0, float)
        draft.text_language = str(raw.get("text_language") or draft.language)
        draft.temperature = _number(raw.get("temperature"), 0.8, float)
        draft.top_p = _number(raw.get("top_p"), 0.9, float)
        draft.max_tokens = _number(raw.get("max_tokens"), 512, int)
        draft.personality = str(raw.get("personality_text") or "")
        draft.speaking_style = str(raw.get("speaking_style_text") or "")
        draft.forbidden_behavior = str(raw.get("forbidden_behavior") or "")
        draft.system_prompt = prompt_path.read_text(encoding="utf-8-sig").strip() if prompt_path.exists() else draft.system_prompt
        avatar = root / str(raw.get("avatar") or "avatar.png")
        draft.avatar_source = str(avatar) if avatar.is_file() else ""
        draft.references = references
        return draft

    def validate(self, draft: CharacterDraft, *, editing_id: str = "") -> CharacterValidation:
        errors: list[str] = []
        warnings: list[str] = []
        character_id = draft.character_id.strip()
        if not _VALID_ID.fullmatch(character_id):
            errors.append("角色 ID 仅支持小写字母、数字、连字符和下划线，且须以字母开头。")
        elif character_id != editing_id and (self.characters_dir / character_id).exists():
            errors.append("角色 ID 已存在。")
        if not draft.display_name.strip():
            errors.append("显示名称不能为空。")
        if not draft.system_prompt.strip():
            errors.append("System prompt 不能为空。")
        if not 0 <= draft.temperature <= 2 or not 0 < draft.top_p <= 1 or draft.max_tokens < 1:
            errors.append("Qwen3 参数无效。")
        if draft.speed <= 0:
            errors.append("语速必须大于 0。")
        if draft.avatar_source and not Path(draft.avatar_source).is_file():
            errors.append("头像文件无法读取。")
        if draft.qwen_model and not self._model_exists("qwen3", character_id, draft.qwen_model):
            errors.append("默认 Qwen3 模型不存在或不属于该角色。")
        elif not draft.qwen_model:
            warnings.append("尚未选择默认 Qwen3 模型。")
        if draft.tts_model and not self._model_exists("gpt_sovits", character_id, draft.tts_model):
            errors.append("默认 GPT-SoVITS 模型不存在或不属于该角色。")
        elif not draft.tts_model:
            warnings.append("尚未选择默认 GPT-SoVITS 模型。")
        neutral = draft.references.get("neutral")
        if neutral is None or not neutral.filename:
            warnings.append("缺少中性参考音频；角色只能保存为未完成状态。")
        for emotion, reference in draft.references.items():
            if not emotion.strip():
                errors.append("情绪名称不能为空。")
            if reference.filename and not reference.text.strip():
                errors.append(f"参考音频“{emotion}”缺少对应文本。")
            if reference.filename and not Path(reference.filename).is_file() and not (self._root_for_existing(editing_id) / "references" / reference.filename).is_file():
                errors.append(f"参考音频“{emotion}”无法读取。")
        return CharacterValidation(tuple(errors), tuple(warnings))

    def save(self, draft: CharacterDraft, *, editing_id: str = "", allow_incomplete: bool = True) -> CharacterValidation:
        result = self.validate(draft, editing_id=editing_id)
        if result.errors:
            raise ValueError("\n".join(result.errors))
        if result.warnings and not allow_incomplete:
            raise ValueError("\n".join(result.warnings))
        target = self._root_for(draft.character_id)
        source = self._root_for_existing(editing_id) if editing_id else None
        if source is not None and source.resolve() != target.resolve():
            raise ValueError("修改角色 ID 请使用“复制角色”，避免覆盖现有资源。")
        target.mkdir(parents=True, exist_ok=True)
        (target / "references").mkdir(exist_ok=True)
        self._copy_resources(target, draft, source)
        self._write_character_files(target, draft)
        return result

    def duplicate(self, source_id: str, new_id: str, new_name: str = "") -> CharacterDraft:
        draft = self.load(source_id)
        draft.character_id = new_id
        draft.display_name = new_name or f"{draft.display_name} 副本"
        draft.qwen_model = ""  # registries remain associated with the source character.
        draft.tts_model = ""
        draft.system_prompt = draft.system_prompt.replace(source_id, new_id)
        self.save(draft, allow_incomplete=True)
        source_root = self._root_for(source_id)
        target_root = self._root_for(new_id)
        for item in ("references", "avatar.png"):
            src = source_root / item
            dst = target_root / item
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.is_file() and not dst.exists():
                shutil.copy2(src, dst)
        return self.load(new_id)

    def delete(self, character_id: str) -> None:
        root = self._root_for(character_id)
        if not root.exists():
            raise FileNotFoundError(f"角色不存在：{character_id}")
        shutil.rmtree(root)

    def export_bundle(self, character_id: str, destination: str | Path) -> Path:
        root = self._root_for(character_id)
        if not root.is_dir():
            raise FileNotFoundError(f"角色不存在：{character_id}")
        bundle = Path(destination)
        if bundle.suffix.lower() != ".zip":
            bundle = bundle.with_suffix(".zip")
        bundle.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(root.parent).as_posix())
        return bundle

    def import_bundle(self, bundle: str | Path, *, replace: bool = False) -> CharacterDraft:
        bundle_path = Path(bundle)
        with zipfile.ZipFile(bundle_path) as archive:
            names = [Path(name) for name in archive.namelist() if not name.endswith("/")]
            if not names:
                raise ValueError("角色包为空。")
            roots = {name.parts[0] for name in names if name.parts}
            if len(roots) != 1 or any(".." in name.parts or name.is_absolute() for name in names):
                raise ValueError("角色包目录结构不安全。")
            character_id = roots.pop()
            if not _VALID_ID.fullmatch(character_id) or not any(name.name == "character.yaml" for name in names):
                raise ValueError("角色包缺少合法的 character.yaml。")
            target = self._root_for(character_id)
            if target.exists() and not replace:
                raise FileExistsError(f"角色已存在：{character_id}")
            with tempfile.TemporaryDirectory(prefix="alysia-character-") as temporary:
                temporary_root = Path(temporary)
                for name in names:
                    output = temporary_root / name
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name.as_posix()) as source, output.open("wb") as dest:
                        shutil.copyfileobj(source, dest)
                imported = temporary_root / character_id
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(imported, target)
        return self.load(character_id)

    def add_reference(self, draft: CharacterDraft, emotion: str, source: str | Path, text: str = "") -> ReferenceVoice:
        emotion = emotion.strip().lower()
        if not emotion:
            raise ValueError("情绪名称不能为空。")
        source_path = Path(source)
        if source_path.suffix.lower() != ".wav":
            raise ValueError("目前仅支持 WAV 参考音频。")
        metrics = inspect_wav(source_path)
        reference = ReferenceVoice(emotion, text.strip(), str(source_path), metrics)
        draft.references[emotion] = reference
        return reference

    def normalize_reference(self, source: str | Path, destination: str | Path) -> WaveMetrics:
        normalize_wav(source, destination)
        return inspect_wav(destination)

    def _write_character_files(self, root: Path, draft: CharacterDraft) -> None:
        prompt = draft.system_prompt.strip() + "\n"
        self._atomic_text(root / "system_prompt.md", prompt)
        self._atomic_text(root / "system_prompt.txt", prompt)  # existing runtime contract
        self._atomic_text(root / "character.yaml", _character_yaml(draft))
        self._atomic_text(root / "speaking_style.yaml", f"description: {_yaml(draft.speaking_style)}\n")
        self._atomic_text(root / "safety.yaml", f"forbidden_behavior: {_yaml(draft.forbidden_behavior)}\n")
        self._atomic_text(root / "reference_texts.yaml", "".join(f"{_yaml_key(key)}: {_yaml(ref.text)}\n" for key, ref in draft.references.items()))
        emotion_map = "".join(
            f"{_yaml_key(key)}: {_yaml(Path(ref.filename).name) if ref.filename else ''}\n"
            for key, ref in draft.references.items()
        )
        self._atomic_text(root / "emotion_map.yaml", emotion_map)

    def _copy_resources(self, root: Path, draft: CharacterDraft, source_root: Path | None) -> None:
        if draft.avatar_source:
            source = Path(draft.avatar_source)
            if source.is_file() and source.resolve() != (root / "avatar.png").resolve():
                shutil.copy2(source, root / "avatar.png")
        for emotion, reference in draft.references.items():
            if not reference.filename:
                continue
            original = Path(reference.filename)
            target = root / "references" / f"{emotion}.wav"
            if original.is_file() and original.resolve() != target.resolve():
                normalize_wav(original, target)
            elif source_root is not None:
                prior = source_root / "references" / original.name
                if prior.is_file() and prior.resolve() != target.resolve():
                    shutil.copy2(prior, target)
            reference.filename = str(target)
            reference.metrics = inspect_wav(target) if target.is_file() else reference.metrics

    def _read_references(self, root: Path) -> dict[str, ReferenceVoice]:
        texts = load_flat_yaml(root / "reference_texts.yaml")
        files = load_flat_yaml(root / "emotion_map.yaml")
        result: dict[str, ReferenceVoice] = {}
        for emotion in set(DEFAULT_EMOTIONS) | set(texts) | set(files):
            filename = str(files.get(emotion) or "")
            path = root / "references" / filename if filename else None
            metrics = None
            if path and path.is_file():
                try:
                    metrics = inspect_wav(path)
                except (OSError, wave.Error, ValueError):
                    pass
            result[str(emotion)] = ReferenceVoice(str(emotion), str(texts.get(emotion) or ""), str(path) if path else "", metrics)
        return result

    def _model_exists(self, kind: str, character_id: str, model_id: str) -> bool:
        path = self.registry_dir / kind / character_id / "models.json"
        try:
            models = json.loads(path.read_text(encoding="utf-8-sig")).get("models", [])
        except (OSError, json.JSONDecodeError):
            return False
        return any(str(model.get("id")) == model_id for model in models if isinstance(model, dict))

    def _active_model_id(self, kind: str, character_id: str) -> str:
        path = self.registry_dir / kind / character_id / "models.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(data.get("active_model_id") or "")

    def _root_for(self, character_id: str) -> Path:
        return self.characters_dir / character_id

    def _root_for_existing(self, character_id: str) -> Path:
        return self._root_for(character_id) if character_id else Path("__no_existing_character__")

    @staticmethod
    def _atomic_text(path: Path, contents: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(contents, encoding="utf-8")
        temporary.replace(path)


def _number(value: object, default: object, typ):
    try:
        return typ(value)
    except (TypeError, ValueError):
        return default


def _yaml(value: object) -> str:
    value = str(value or "")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _yaml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _yaml(value)


def _character_yaml(draft: CharacterDraft) -> str:
    rows: Iterable[tuple[str, object]] = (
        ("character_id", draft.character_id), ("id", draft.character_id), ("name", draft.display_name),
        ("display_name", draft.display_name), ("description", draft.description), ("avatar", "avatar.png"),
        ("language", draft.language), ("default_reply_mode", draft.reply_mode), ("history_turns", draft.history_turns),
        ("memory_enabled", str(draft.memory_enabled).lower()), ("qwen3_default_model", draft.qwen_model),
        ("temperature", draft.temperature), ("top_p", draft.top_p), ("max_tokens", draft.max_tokens),
        ("personality_text", draft.personality), ("speaking_style_text", draft.speaking_style),
        ("forbidden_behavior", draft.forbidden_behavior), ("gpt_sovits_default_model", draft.tts_model),
        ("default_emotion", draft.default_emotion), ("speed", draft.speed), ("text_language", draft.text_language),
    )
    content = "".join(f"{key}: {_yaml(value) if isinstance(value, str) else value}\n" for key, value in rows)
    # Keep the existing Qwen3 profile reader useful for newly-created roles,
    # while retaining the editor's free-form fields above.
    personality = [item.strip() for item in re.split(r"[,\n]", draft.personality) if item.strip()]
    forbidden = [item.strip() for item in re.split(r"[,\n]", draft.forbidden_behavior) if item.strip()]
    content += "personality:\n" + "".join(f"  - {_yaml(item)}\n" for item in personality)
    content += "speaking_style:\n"
    content += f"  max_sentences: {max(1, min(10, draft.history_turns))}\n"
    content += "  use_spoken_language: true\n"
    content += "forbidden:\n" + "".join(f"  - {_yaml(item)}\n" for item in forbidden)
    return content
