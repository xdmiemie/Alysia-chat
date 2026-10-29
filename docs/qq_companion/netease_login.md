# 网易云账号登录

后台服务可以保持关闭。使用已经安装的 AstrBot Python 环境运行：

```powershell
envs/astrbot/Scripts/python.exe scripts/qq_companion/netease_login.py
```

脚本通过 HTTPS 直接请求 `music.163.com`，生成二维码图片
`storage/qq_companion/auth/login/qr.png`。打开图片，用网易云音乐手机 App
扫描，并在手机上确认登录。无需在终端、聊天或提示词里填写账号密码。
二维码默认等待 240 秒，网易云也可能提前使其过期；过期后重新运行脚本。
脚本结束时会清理临时二维码。

如果手机确认后返回 `netease_qr_poll_rejected_8821`，这是网易云的安全环境验证，
不要反复生成二维码。可在官方网页完成登录后，使用本机一次性导入页：

```powershell
envs/astrbot/Scripts/python.exe scripts/qq_companion/netease_cookie_import.py
```

打开命令输出的 `http://127.0.0.1:端口/`。页面包含具体复制步骤：浏览器 F12 →
应用 / Application → 存储 → Cookies → `https://music.163.com`，复制 `MUSIC_U`
的 Value 和可选 `__csrf` 的 Value，粘贴到导入页，点击验证并加密保存。
不要发到聊天或写入项目配置。导入页仅监听本机回环地址，要求同源表单和随机
校验值，不记录输入；验证成功后退出，最长等待 30 分钟。此方式与扫码登录使用
相同的加密凭据位置。二维码轮询网络失败后可用 `netease_login.py --resume`
继续尚未过期的授权，但该选项不能绕过网易云行为验证。

授权成功后，脚本用获得的 Cookie 再查询一次账号状态，验证成功才保存到
`storage/qq_companion/auth/netease.dpapi`。这是 Windows DPAPI 当前用户加密的文件，
不能作为明文 Cookie 读取，不能复制到另一 Windows 账号使用。目录已被 Git 忽略。
`ASTRBOT_CHAT_STATE_DIR` 若有设置，文件随该状态目录存放。

后续按原来的方式启动大肥鱼即可。媒体服务和独立歌曲进程通过同一个
`NetEaseSource.from_environment()` 入口自动解密读取，不需要每次扫码、设置环境变量，
也不需要在提示词里加入账号信息。Cookie 只用于网易云接口，不会传给音频 CDN。
只有账号本身可获取的完整音源才会使用，试听仍被排除。

获取播放地址时，服务会在当前账号 Cookie 中声明 `os=pc`，仅对该请求生效，
不会修改已保存凭据。仅带 `MUSIC_U` 的网页登录上下文可能返回含 `authSecret`
的地址：接口及歌曲状态均为 200，实际音频 CDN 却返回 403，甚至出现匿名可下载、
登录后失败的情况。2026-10-29 用同一账号对照测试，声明桌面客户端后两版《雨爱》、
《奇迹再现》和《叹云兮》均可获取完整音频；补 Referer、Origin 或浏览器 UA 未解决
原地址的 403。服务使用接口重新签发的地址，保留账号权限检查和试听过滤。

检查状态，不发起新登录：

```powershell
envs/astrbot/Scripts/python.exe scripts/qq_companion/netease_login.py --status
```

更换账号或会话失效时重新扫码：

```powershell
envs/astrbot/Scripts/python.exe scripts/qq_companion/netease_login.py --force
```

新账号验证成功前保留原凭据。已运行的服务需要重启才能统一使用新账号。
自动读取保存的会话不代表会话永久有效；网易云使其失效后仍需重新扫码。
脚本和状态检查只输出登录状态、昵称和 `vip_type`，不会输出 Cookie。
凭据损坏或无法解密时会报错，不会悄悄改成匿名账号。

兼容手动环境变量，读取顺序：`ASTRBOT_CHAT_NETEASE_COOKIE`、
`ALYSIA_NETEASE_COOKIE`、加密凭据文件。已有环境变量会覆盖保存的账号，
切换到本机保存方式时应清除旧变量。不要把 Cookie 放到受版本控制的配置文件中。

扫码图片依赖 `qrcode==8.2`（现有 AstrBot 环境已安装）；媒体运行环境只读取
加密凭据，不需要安装扫码依赖。扫码协议参考 API Enhanced 的
[生成 key](https://github.com/neteasecloudmusicapienhanced/api-enhanced/blob/main/module/login_qr_key.js)、
[检测授权](https://github.com/neteasecloudmusicapienhanced/api-enhanced/blob/main/module/login_qr_check.js)
和 [二维码内容](https://github.com/neteasecloudmusicapienhanced/api-enhanced/blob/main/module/login_qr_create.js)
源代码，项目不运行该第三方代理服务。
