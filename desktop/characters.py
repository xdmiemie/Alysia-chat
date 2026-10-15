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
from datetime import datetime, timezone
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
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 256
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


@dataclass(frozen=True)
class SharedModel:
    """One model artifact with one or more character registry associations."""

    model_type: str
    model_id: str
    registry_key: str
    display_name: str
    kind: str
    required_files: tuple[Path, ...]
    character_ids: tuple[str, ...]
    raw: dict[str, object]


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
        draft.qwen_model = str(self._active_model_id("qwen3", character_id) or raw.get("qwen3_default_model") or "")
        draft.tts_model = str(self._active_model_id("gpt_sovits", character_id) or raw.get("gpt_sovits_default_model") or "")
        draft.default_emotion = str(raw.get("default_emotion") or "neutral")
        draft.speed = _number(raw.get("speed"), 1.0, float)
        draft.text_language = str(raw.get("text_language") or draft.language)
        draft.temperature = _number(raw.get("temperature"), 0.7, float)
        draft.top_p = _number(raw.get("top_p"), 0.9, float)
        draft.max_tokens = _number(raw.get("max_tokens"), 256, int)
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

    # Model entries intentionally stay in the per-character registry format
    # consumed by the runtime.  The same raw entry may occur in several
    # registries and points at one physical artifact directory, which makes a
    # model shareable without adding a parallel runtime registry.
    def list_shared_models(self, model_type: str) -> list[SharedModel]:
        self._check_model_type(model_type)
        grouped: dict[str, tuple[dict[str, object], set[str]]] = {}
        for character_id, manifest in self._iter_model_manifests(model_type):
            for value in manifest.get("models", []):
                if not isinstance(value, dict) or not value.get("id"):
                    continue
                model_id = str(value["id"])
                registry_key = self._model_registry_key(model_id, value.get("required_files", []))
                if registry_key not in grouped:
                    grouped[registry_key] = (dict(value), set())
                grouped[registry_key][1].add(character_id)
        models: list[SharedModel] = []
        for registry_key, (raw, character_ids) in grouped.items():
            model_id = str(raw["id"])
            files = tuple(self._project_model_path(path) for path in raw.get("required_files", []) if path)
            models.append(SharedModel(
                model_type=model_type,
                model_id=model_id,
                registry_key=registry_key,
                display_name=str(raw.get("display_name") or model_id),
                kind=str(raw.get("kind") or "trained"),
                required_files=files,
                character_ids=tuple(sorted(character_ids)),
                raw=raw,
            ))
        return sorted(models, key=lambda item: (item.kind != "base", item.display_name.lower(), item.model_id))

    def list_character_models(self, model_type: str, character_id: str) -> list[SharedModel]:
        """Use the same per-character registry as the runtime selectors."""
        return [model for model in self.list_shared_models(model_type) if character_id in model.character_ids]

    def associate_model(self, model_type: str, model_key: str, character_ids: Iterable[str]) -> SharedModel:
        """Associate an existing model with roles, reusing its physical files."""
        self._check_model_type(model_type)
        source = self._shared_model(model_type, model_key)
        model_key = source.registry_key
        ids = self._valid_character_ids(character_ids)
        if not ids:
            raise ValueError("请至少选择一个角色。")
        for character_id in ids:
            manifest = self._load_model_manifest(model_type, character_id)
            if any(str(item.get("id")) == source.model_id and
                   self._model_registry_key(str(item.get("id") or ""), item.get("required_files", [])) != model_key
                   for item in manifest["models"]):
                raise ValueError(f"角色“{character_id}”已有同 ID 的不同模型。")
            models = [item for item in manifest["models"] if self._model_registry_key(str(item.get("id") or ""), item.get("required_files", [])) != model_key]
            models.append(dict(source.raw))
            manifest["models"] = models
            if not manifest.get("active_model_id"):
                manifest["active_model_id"] = source.model_id
            self._save_model_manifest(model_type, character_id, manifest)
        return next(model for model in self.list_shared_models(model_type) if model.registry_key == model_key)

    def add_shared_model(
        self,
        model_type: str,
        model_id: str,
        display_name: str,
        character_ids: Iterable[str],
        required_files: Iterable[str | Path],
        *,
        model_kind: str = "trained",
        model_format: str = "gguf",
    ) -> SharedModel:
        """Import model files once, then associate their registry entry with roles."""
        self._check_model_type(model_type)
        model_id = model_id.strip()
        if not _VALID_ID.fullmatch(model_id):
            raise ValueError("模型 ID 仅支持小写字母、数字、连字符和下划线，且须以字母开头。")
        if any(model.model_id == model_id for model in self.list_shared_models(model_type)):
            raise ValueError("模型 ID 已存在；请使用已有模型的角色关联功能，或填写新的 ID。")
        if model_kind not in {"base", "trained"}:
            raise ValueError("模型类型必须是 base 或 trained。")
        ids = self._valid_character_ids(character_ids)
        files = [Path(path) for path in required_files if str(path).strip()]
        if not ids:
            raise ValueError("请至少选择一个角色。")
        if not files or any(not path.is_file() for path in files):
            raise ValueError("请选择存在的模型文件。")
        suffixes = {path.suffix.lower() for path in files}
        if model_type == "gpt_sovits" and not {".ckpt", ".pth"}.issubset(suffixes):
            raise ValueError("TTS 模型必须同时选择 .ckpt 和 .pth 文件。")
        if model_type == "qwen3" and not suffixes.intersection({".gguf", ".bin", ".safetensors"}):
            raise ValueError("Qwen 模型需要至少一个 .gguf、.bin 或 .safetensors 文件。")

        target_dir = self.registry_dir / model_type / "_shared" / model_id
        if target_dir.exists() and any(target_dir.iterdir()):
            raise FileExistsError(f"模型目录已存在，拒绝覆盖文件：{target_dir}")
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for source in files:
            target = target_dir / source.name
            if source.resolve() != target.resolve():
                shutil.copy2(source, target)
            copied.append(target)
        raw: dict[str, object] = {
            "id": model_id,
            "display_name": display_name.strip() or model_id,
            "kind": model_kind,
            "required_files": [self._relative_model_path(path) for path in copied],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if model_type == "qwen3":
            raw["format"] = model_format
        for character_id in ids:
            manifest = self._load_model_manifest(model_type, character_id)
            manifest["models"] = [item for item in manifest["models"] if str(item.get("id")) != model_id] + [dict(raw)]
            if not manifest.get("active_model_id"):
                manifest["active_model_id"] = model_id
            self._save_model_manifest(model_type, character_id, manifest)
        return next(model for model in self.list_shared_models(model_type) if model.registry_key == self._model_registry_key(model_id, raw["required_files"]))

    def remove_model_association(self, model_type: str, model_key: str, character_id: str) -> bool:
        """Remove one role's association and delete unreferenced owned files.

        Returns ``True`` only when no role retains the model, so the caller can
        show the final-file-removal notice.
        """
        self._check_model_type(model_type)
        manifest = self._load_model_manifest(model_type, character_id)
        model = self._shared_model(model_type, model_key)
        model_key = model.registry_key
        removed = [item for item in manifest["models"] if self._model_registry_key(str(item.get("id") or ""), item.get("required_files", [])) == model_key]
        if not removed:
            raise ValueError(f"角色“{character_id}”未使用该模型。")
        model_id = str(removed[0].get("id") or "")
        if model_id in {str(manifest.get("active_model_id") or ""),
                        str(manifest.get("previous_model_id") or "")}:
            raise ValueError("当前模型或上一回滚版本不能删除；请先切换并建立新的回滚版本。")
        manifest["models"] = [item for item in manifest["models"] if self._model_registry_key(str(item.get("id") or ""), item.get("required_files", [])) != model_key]
        self._save_model_manifest(model_type, character_id, manifest)
        config_path = self._root_for(character_id) / "character.yaml"
        field = "qwen3_default_model" if model_type == "qwen3" else "gpt_sovits_default_model"
        if config_path.is_file() and str(load_flat_yaml(config_path).get(field) or "") == model_id:
            contents = config_path.read_text(encoding="utf-8-sig")
            replacement = f"{field}: {_yaml(str(manifest.get('active_model_id') or ''))}"
            self._atomic_text(config_path, re.sub(rf"(?m)^{field}:.*$", replacement, contents))

        still_used = any(model.registry_key == model_key for model in self.list_shared_models(model_type))
        if still_used:
            return False
        referenced = {
            path.resolve() for model in self.list_shared_models(model_type) for path in model.required_files
        }
        owned_root = (self.registry_dir / model_type).resolve()
        owned_directories: set[Path] = set()
        for raw in removed:
            for value in raw.get("required_files", []):
                path = self._project_model_path(value)
                for directory in path.resolve().parents:
                    if directory == owned_root:
                        break
                    if directory.name == model_id and owned_root in directory.parents:
                        owned_directories.add(directory)
                        break
                if path.resolve() not in referenced:
                    self._remove_owned_model_file(model_type, path)
        for directory in owned_directories:
            if directory.is_dir() and not any(path == directory or directory in path.parents for path in referenced):
                shutil.rmtree(directory)
        return True

    def export_bundle(self, character_id: str, destination: str | Path, *, include_models: bool = False) -> Path:
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
            model_files: dict[str, str] = {}
            for model_type in ("qwen3", "gpt_sovits"):
                manifest = self._load_model_manifest(model_type, character_id)
                archive.writestr(f"{character_id}/model_manifests/{model_type}.json",
                                 json.dumps(manifest, ensure_ascii=False, indent=2))
                if include_models:
                    for raw in manifest["models"]:
                        model_id = str(raw.get("id") or "")
                        if not _VALID_ID.fullmatch(model_id):
                            raise ValueError(f"模型 ID 不合法：{model_id}")
                        for index, value in enumerate(raw.get("required_files", [])):
                            source = self._project_model_path(value)
                            if not source.is_file():
                                raise FileNotFoundError(f"模型文件缺失：{source}")
                            key = str(value)
                            if key not in model_files:
                                bundled = f"{character_id}/model_files/{model_type}/{model_id}/{index}_{source.name}"
                                archive.write(source, bundled)
                                model_files[key] = bundled
            archive.writestr(f"{character_id}/bundle_metadata.json", json.dumps({
                "format": 2, "character_id": character_id, "includes_models": include_models,
                "model_files": model_files,
            }, ensure_ascii=False, indent=2))
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
            if not replace and any((self.registry_dir / kind / character_id / "models.json").exists()
                                   for kind in ("qwen3", "gpt_sovits")):
                raise FileExistsError(f"角色“{character_id}”的模型清单已存在。")
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
                shutil.copytree(imported, target,
                                ignore=shutil.ignore_patterns("model_manifests", "model_files", "bundle_metadata.json"))
                metadata_path = imported / "bundle_metadata.json"
                metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
                bundled_files = metadata.get("model_files") or {}
                installed_files: dict[str, str] = {}
                for model_type in ("qwen3", "gpt_sovits"):
                    manifest_path = imported / "model_manifests" / f"{model_type}.json"
                    if not manifest_path.is_file():
                        continue
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    for raw in manifest.get("models", []):
                        model_id = str(raw.get("id") or "")
                        if not _VALID_ID.fullmatch(model_id):
                            raise ValueError(f"模型 ID 不合法：{model_id}")
                        restored: list[str] = []
                        for index, value in enumerate(raw.get("required_files", [])):
                            bundled = bundled_files.get(str(value))
                            if bundled:
                                if bundled in installed_files:
                                    restored.append(installed_files[bundled])
                                    continue
                                source = temporary_root / bundled
                                if not source.is_file() or temporary_root.resolve() not in source.resolve().parents:
                                    raise ValueError("角色包模型路径不安全或文件缺失。")
                                destination = self.registry_dir / model_type / character_id / model_id / f"{index}_{source.name}"
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copy2(source, destination)
                                installed_files[bundled] = self._relative_model_path(destination)
                                restored.append(installed_files[bundled])
                            else:
                                restored.append(str(value))
                        raw["required_files"] = restored
                    self._save_model_manifest(model_type, character_id, manifest)
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

    def _check_model_type(self, model_type: str) -> None:
        if model_type not in {"qwen3", "gpt_sovits"}:
            raise ValueError(f"不支持的模型类型：{model_type}")

    def _valid_character_ids(self, character_ids: Iterable[str]) -> list[str]:
        ids = sorted({str(value).strip() for value in character_ids if str(value).strip()})
        known = {character_id for character_id, _ in self.list_profiles()}
        invalid = [character_id for character_id in ids if character_id not in known]
        if invalid:
            raise ValueError("角色不存在：" + "、".join(invalid))
        return ids

    def _iter_model_manifests(self, model_type: str) -> Iterable[tuple[str, dict[str, object]]]:
        root = self.registry_dir / model_type
        if not root.exists():
            return
        for path in sorted(root.glob("*/models.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            models = manifest.get("models")
            if isinstance(models, list):
                yield path.parent.name, {"character_id": path.parent.name, **manifest, "models": models}

    def _load_model_manifest(self, model_type: str, character_id: str) -> dict[str, object]:
        path = self.registry_dir / model_type / character_id / "models.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            raw = {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型清单无法解析：{path}") from exc
        models = raw.get("models")
        if not isinstance(models, list):
            models = []
        return {**raw, "character_id": character_id, "models": models}

    def _save_model_manifest(self, model_type: str, character_id: str, manifest: dict[str, object]) -> None:
        path = self.registry_dir / model_type / character_id / "models.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**manifest, "character_id": character_id}
        self._atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def _project_model_path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.project_root / path

    def _model_registry_key(self, model_id: str, required_files: object) -> str:
        values = required_files if isinstance(required_files, list) else []
        paths = "\x1f".join(sorted(str(self._project_model_path(item).resolve()) for item in values if item))
        return f"{model_id}\x1e{paths}"

    def _shared_model(self, model_type: str, key_or_id: str) -> SharedModel:
        models = self.list_shared_models(model_type)
        exact = next((model for model in models if model.registry_key == key_or_id), None)
        if exact is not None:
            return exact
        matches = [model for model in models if model.model_id == key_or_id]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError("存在同名但文件不同的模型，请从管理界面选择具体模型。")
        raise ValueError("模型不存在。")

    def _relative_model_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    def _remove_owned_model_file(self, model_type: str, path: Path) -> None:
        """Only clean files located in this application's model registry."""
        root = (self.registry_dir / model_type).resolve()
        resolved = path.resolve()
        if not resolved.is_file() or root not in resolved.parents:
            return
        resolved.unlink()
        current = resolved.parent
        while current != root and current.exists():
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

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
