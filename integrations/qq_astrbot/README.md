# AstrBot integration

The QQ phase plan is in `astrbot-chat项目阶段计划.md`. AstrBot is the intended
message and Agent owner; SnowLuma provides OneBot v11. The `plugin/` directory
will be added when the AstrBot plugin is implemented and tested.

`reference/` preserves partial song and Feiyu RVC code from the earlier QQ
adapter. It is not an installable AstrBot plugin. Some imports require modules
that exist only on the old `qq` branch. Keep its song and RVC behavior as a
reference during Q4 migration; do not load it as a second QQ Agent.
