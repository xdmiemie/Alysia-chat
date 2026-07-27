# GPT-SoVITS Voice Dataset Format

Stage 8 expects private WAV voice data under a per-character raw directory:

```text
training/gpt_sovits/datasets/<character>/raw/
  any_name_you_like.wav
  clips/
    another_name.wav
```

The raw directory is intentionally ignored by Git. Put real voice data there
locally; do not commit licensed or private audio unless it has been explicitly
cleared for distribution. File names are not part of the training contract; the
pipeline recursively reads every `.wav` under `raw/`.

The pipeline uses the project SenseVoice recognizer to create:

```text
training/gpt_sovits/datasets/<character>/work/manifest.jsonl
training/gpt_sovits/datasets/<character>/work/manifest.normalized.jsonl
training/gpt_sovits/datasets/<character>/work/validation_report.json
```

Each generated `manifest.jsonl` line describes one clip:

```json
{"audio_path":"clips/000001.wav","text":"准确台词","speaker":"elysia","emotion":"neutral","language":"zh","source":"manual","duration":5.2}
```

Use short, clean clips: roughly 1-12 seconds, no background music, no overlap.
SenseVoice generates first-pass transcripts; review `work/manifest.jsonl` before
expensive real training if quality matters.

Prepare and validate before training:

```powershell
python training/gpt_sovits/preprocess/dataset_manifest.py --config training/gpt_sovits/configs/characters/elysia.yaml --prepare
```
