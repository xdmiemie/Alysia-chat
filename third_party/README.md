# Third-Party Source Checkouts

Stage 0 reserves a fixed checkout location for upstream projects that must be
used from source:

- `third_party/GPT-SoVITS`

SnowLuma and Applio local installations for the QQ phase live under
`external/` and are ignored by Git.

The pinned upstream repository and commit fields are tracked in
`configs/third_party.yaml`.

SenseVoice runs through the `runtime/sensevoice` FunASR integration and local
`model_store` model manifest. llama.cpp is installed as project-local runtime
tools under `tools/llama.cpp`.
