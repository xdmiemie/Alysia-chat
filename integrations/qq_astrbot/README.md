# AstrBot integration

Plugin 0.6.0 adds Q4 voice routing, weapi original playback, whole-song Feiyu covers and low-frequency stickers. See [Q4 operations and local-only acceptance](../../docs/qq_companion/q4.md). All intent decisions come from the same Agent reply; no keyword classifier or song-selection menu is used.

The QQ phase plan is in `astrbot-chat项目阶段计划.md`. AstrBot is the intended
message and Agent owner; SnowLuma provides OneBot v11. `plugin/` contains the
Q1 persistent inbound guard, Q2 `q2_echo` tool, Q3 scoped routing and permissions,
and paced QQ text replies. Primary replies send at most three messages in a
group or six in private chat; remaining sentences are combined into the last
message. Private replies may add one optional natural follow-up, saved in
AstrBot's conversation history. Groups do not add optional follow-ups. Only AstrBot's Runner owns
QQ history and calls the local Qwen3 provider. The plugin never invokes
`AstrbotChatCore.process`.

Q3 maps history to `qq:{bot_uin}:dm:{sender_uin}` or
`qq:{bot_uin}:group:{group_id}` before the Runner reads a conversation. Group
prompts use only a bounded, structured TTL window carrying numeric speaker IDs,
names, mentions and reply IDs. Unknown tools are excluded; registered public
tools have an execution guard. Group controls and outbound quotas persist in
SQLite. See [operations](../../docs/qq_companion/operations.md) for configuration,
administrator commands, and local acceptance commands. No QQ connection is
needed for `scripts/qq_companion/probe_q3_local.py`.

All fixed prompts for the current QQ main-reply and private-follow-up path are
editable in [`configs/qq_companion/prompts.yaml`](../../configs/qq_companion/prompts.yaml),
with comments describing every section. This includes the active AstrBot
safety/tool/date blocks and the echo tool descriptions. `prompt_loader.py` reads
that same project file on each request, including from the installed plugin;
there is no second editable copy. Persona prompts keep their existing location.

`reference/` preserves partial song and Feiyu RVC code from the earlier QQ
adapter. It is not an installable AstrBot plugin. Some imports require modules
that exist only on the old `qq` branch. Keep its song and RVC behavior as a
reference during Q4 migration; do not load it as a second QQ Agent.
