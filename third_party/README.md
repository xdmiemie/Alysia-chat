# Third-Party Source Checkouts

Stage 0 reserves fixed locations for upstream projects:

- `third_party/SenseVoice`
- `third_party/llama.cpp`
- `third_party/GPT-SoVITS`

The pinned upstream repository and commit fields are tracked in
`configs/third_party.yaml`. The actual source trees are intentionally not
downloaded by the stage 0 bootstrap because they are large and environment
specific.
