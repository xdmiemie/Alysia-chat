# Q0 dependency lock and license review

Q4 reuses the installed model runtime: requests 2.34.2, pycryptodome 3.23.0, Pillow 10.4.0 and psutil 7.2.2 (pinned in `services/media_api/requirements.txt`); the isolated AstrBot environment already provides psutil 7.1.3. No runtime package upgrade was performed. Feiyu/Applio/audio-separator source, weight and compatibility pins remain those recorded in `qq_companion/stage14a.md`. The matsuko_cover UI was reviewed but is not a new runtime dependency.

Recorded 2026-10-28 (Asia/Hong_Kong). Git SHA or release tag is the source lock; binaries below also have local SHA256. These are local verification values, not upstream published checksums unless explicitly noted. Model weights, audio references, account data and secrets stay outside Git.

| Component | Pinned source / local version | License / boundary | Verification |
|---|---|---|---|
| astrbot-chat Stage 10 | `aca495e801f061210ce4961da8df2f63ab3f3c44` | Existing project | `git merge-base` verified on `astrbot-chat` |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.29.3) | v4.29.3, `f61a7f42696da6df25dc7a249ec9b8d54ed9da18` | [AGPL-3.0-or-later](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.3/LICENSE); keep source/obligations for redistribution or network service | Source under ignored `external/AstrBot`; Python 3.12 isolated venv |
| [SnowLuma](https://github.com/SnowLuma/SnowLuma/releases/tag/v2.1.1) | v2.1.1 Windows x64, source commit `f8cb15c` shown on release | [Source-available noncommercial](https://github.com/SnowLuma/SnowLuma), plus bundled binary EULA/privacy; commercial use and public modified distribution need written authorization | Archive SHA256 `C739171F95C27706767E69BA36099707C222FCEC639B46F4D59689997B527772` from local install record |
| Python | Model runtime 3.10.11; AstrBot 3.12.10 | [PSF License](https://docs.python.org/3/license.html) | Official 3.12.10 installer local SHA256 `67B5635E80EA51072B87941312D00EC8927C4DB9BA18938F7AD2D27B328B95FB` |
| Node.js | 24.22.1 bundled by SnowLuma | Subject to SnowLuma distribution terms and Node license | `external/SnowLuma/v2.1.1/node.exe --version` |
| FFmpeg | 8.1.2 essentials build | Build flags include `--enable-gpl --enable-version3`; review [FFmpeg GPLv3 distribution obligations](https://ffmpeg.org/legal.html) and preserve bundled notices | `ffmpeg.exe` local SHA256 `1326DDE4C84FF1F96FE6B8916C5BED29E163E9B5DCCF995F6F3DB069D143EC5E` |
| llama.cpp | Windows CUDA binary release `b10099`; `llama-server --version` reports build 10099 (`1a064ab09`) | [MIT](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE) | `llama-server.exe` local SHA256 `2B59F51676F1FB22A68D4235B41BA4DDFAEECE0F755B6F3256FA0E98438FEBC9` |
| GPT-SoVITS | Local source `d523079fc05d9a8028d6085bffe4a2757c32abb6` | [MIT](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/LICENSE); voice samples/weights require separate rights | Stage 10 source kept in `third_party/GPT-SoVITS` |
| [Qwen3-14B-GGUF](https://huggingface.co/Qwen/Qwen3-14B-GGUF) | Q4_K_M, active manifest `qwen3-14b-q4-k-m-base-v1` | Apache-2.0 model card; local GGUF file excluded from Git | Local model SHA256 `500A8806E85EE9C83F3AE08420295592451379B4F8CF2D0F41C15DFFEB6B81F0` |
| [Qzone plugin](https://github.com/Zhalslar/astrbot_plugin_qzone) | Not installed in Q0/Q1 | GPL-3.0; Q6 gate before use | No runtime dependency in Q1 |

Local GPT-SoVITS Bailu weights SHA256: `Bailu-e10.ckpt` = `9DDD1C0B4627DF4370C2658E0791B0F5F5D98749B92DC33EC2A6E5167F1AFEA2`; `Bailu_e10_s820.pth` = `54BEAB46EE54A0CD0C969132EF9C608629484ABCAC777C00C9446414BC6F7E80`. Local SenseVoice `model.pt` SHA256 = `833CA2DCFDF8EC91BD4F31CFAC36D6124E0C459074D5E909AEC9CABE6204A3EA`. These local hashes establish the Q0 rollback copies but do not verify model download provenance or rights to the voice.

SnowLuma's bundled EULA section 5.4 explicitly requires prior written authorization for incorporating proprietary components in a third-party installer or Docker image and for deployment through an automated script. This repository does not include or modify its binary; the local start script launches the user's already installed official release. Resolve the EULA's automated-deployment restriction with its copyright holder before distributing a packaged installer or deployment automation.

The installed CUDA driver reports 616.56; PyTorch in the existing runtime reports 2.11.0+cu128. AstrBot's installed transitive versions are frozen in [astrbot-pip-freeze.txt](qq_companion/astrbot-pip-freeze.txt); `pip check` found no broken requirements. Keep this alongside the source SHA when recreating the isolated venv. Hashes for every PyPI wheel are not recorded; do not silently upgrade the isolated venv.
