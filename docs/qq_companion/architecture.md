# astrbot-chat QQ directory map

- `runtime/`, `training/`, and `model_store/`: retained Stage 0-10 local core,
  LoRA pipeline, model manifests, and local model assets.
- `integrations/qq_astrbot/`: AstrBot plugin home. `reference/` holds the
  incomplete old QQ adapter for migration, not an active plugin.
- `scripts/qq_companion/`: QQ model preparation utilities. Q0-Q2 service
  lifecycle scripts are still planned.
- `configs/qq_companion/`: model and OneBot integration example. `local.json`
  is a local legacy configuration and is ignored by Git.
- `docs/qq_companion/`: current architecture notes and the historical Stage 14A
  Feiyu RVC record.
- `external/`: ignored local SnowLuma and Applio installations. The older
  GPT-SoVITS source remains at `third_party/GPT-SoVITS` for existing scripts.
- `storage/qq_companion/`: ignored QQ state and media. `legacy_qq/` contains
  data moved from the old adapter. `model_store/rollback/` holds the previous
  elysia TTS weights locally.

The planned `services/`, `companion/`, and AstrBot `plugin/` modules are created
when their Q stages are implemented, so the tree does not imply that those
services already run.
