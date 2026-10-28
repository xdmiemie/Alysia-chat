# AstrBot integration

The QQ phase plan is in `astrbot-chat项目阶段计划.md`. AstrBot is the intended
message and Agent owner; SnowLuma provides OneBot v11. `plugin/` contains the
Q1 persistent inbound guard, Q2 `q2_echo` tool, and paced QQ text replies. Each
model reply is split into one sentence per outgoing QQ message. After those
messages, one separate local Qwen3 call may choose a single natural follow-up;
any sent follow-up is saved in AstrBot's conversation history. Only AstrBot's Runner owns
QQ history and calls the local Qwen3 provider. The plugin never invokes
`AstrbotChatCore.process`.

`reference/` preserves partial song and Feiyu RVC code from the earlier QQ
adapter. It is not an installable AstrBot plugin. Some imports require modules
that exist only on the old `qq` branch. Keep its song and RVC behavior as a
reference during Q4 migration; do not load it as a second QQ Agent.
