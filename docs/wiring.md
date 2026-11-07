# 接线说明：板子 → 设备服务 → bothouse

```
  板子 ──(1) OTA: POST http://<设备服务>:8791/xiaozhi/ota/ ──▶ 设备服务 回 WebSocket 地址
       ──(2) WS:  ws://<设备服务>:8791/xiaozhi/v1/ (Bearer 钥匙) ──▶ 设备服务
  设备服务 ──(3) ws://<bothouse>:9810/bridge/ws (Bearer bridge token) ──▶ bothouse ──▶ [[boards]].project 的 bot
```

## 1. bothouse 侧

在 bothouse 的 `config.toml` 打开外部适配器接口：

```toml
[bridge]
enabled = true
port = 9810
token = "一个强随机密钥"     # 设备服务用它连进来
```

每个角色要有一个项目（`[[projects]]`），名字写进设备服务配置的 `[[boards]].project`。

语音默认交给 bothouse，bothouse 没配时自动退到本机命令（设备服务 `[speech]`）：

- 默认（`stt = "bothouse"`、`tts = "bothouse"`）：bothouse 配了 `[speech]`（转写）和 `[tts]`（`tts_mode = "always"`）时，
  板子的语音以 `audio` 发给 bothouse，bot 的回话由 bothouse 合成后以 `audio` 发回。
  bothouse 没配 `[speech]`（回「语音未启用」）时，改用 `stt_command` 本机转写，刚才那句重转一次；
  回话 20 秒没等到 bothouse 的音频时，改用 `tts_command` 本机合成（不写时用 macOS `say` 或 Linux `espeak-ng`）。
  退过一次就一直用本机，直到设备服务重新连上 bothouse。
- 只用本机命令：`stt = "command"` / `tts = "command"`，配 `stt_command` / `tts_command`（见 `config.example.toml`），
  bothouse 只收发文字。

bothouse 在回话末尾加的状态脚注、Markdown 记号不会念出来；bothouse 的「正在转写」提示也不念。

## 2. 固件

```bash
. $IDF_PATH/export.sh                              # ESP-IDF v6.1
OTA_URL=http://<设备服务的局域网 IP>:8791/xiaozhi/ota/ firmware/scripts/build.sh
```

脚本会拉干净的 xiaozhi-esp32 v2.5.2、打 `firmware/patches/`、放入表盘组件、把 OTA 地址编进去，产物在
`firmware/.build/out/`。刷写见 [firmware/README.md](../firmware/README.md)。表盘只接在 spotpear sp-esp32-s3-1.28-box 上。

已经刷好的板子不用重刷，改服务器地址用 `tools/board-nvs.py --ota-url ...`。

## 3. 设备服务

```bash
cd server && uv venv && uv pip install -e .
cp ../config.example.toml ../config.toml
BOTHOUSE_BRIDGE_TOKEN=<bridge token> .venv/bin/python -m bothouse_iot --config ../config.toml
```

依赖 ffmpeg（编解码 Opus、转头像）。启动后日志里应有：

```
[xiaozhi] 小智协议服务 http://0.0.0.0:8791/xiaozhi/ota/  已绑定的板子：...
[bothouse] 注册成功：平台 iot，能力 text,audio,typing，板子 ju→my-project
```

bothouse 没起来时设备服务按 1→60 秒退避重连，第一次失败记一行，之后 10 分钟内不再重复记。

## 4. 绑定

板子插电联网后会来 OTA，没绑定的板子日志里会打出它的 MAC：

```
[xiaozhi] 未绑定的板子 xx:xx:xx:xx:xx:xx 连上了 ... 要用就在配置 [[boards]] 写 device = "..." 和 role
```

把它写进 `config.toml` 的 `[[boards]]`（一块板绑一个角色、一个 bothouse 项目），重启设备服务。
所有绑定只在这一张表里。

## 鉴权

- 局域网：`[device] token` 可留空，或设一个共用 token（OTA 回包会下发给板子）。
- 每块板一把钥匙（可选）：`tools/board-nvs.py --port <串口> --ws-token-gen` 生成钥匙写进板子 NVS（`websocket:token`），
  把打印出的钥匙写进这块板的 `[[boards]].key`。OTA 回包对这块板不再带 token（固件只覆盖回包里有的键，NVS 里的钥匙保留），
  WebSocket 只认这把钥匙。
- 对外网开放（`public_url` 配了反向代理）时建议 `require_key = true`：没配钥匙的板子 OTA 和 WebSocket 都拒绝。

## 自检

不接真板子，用假板子对着一个隔离的 bothouse 跑一圈：

```bash
BOTHOUSE_BRIDGE_TOKEN=... server/.venv/bin/python tools/e2e.py --bothouse ws://127.0.0.1:9810/bridge/ws --project <项目>
```

它会验证：没钥匙的板子被拒、有钥匙的连上、说一句话发到 bothouse 的项目、bot 回话变成 Opus 帧推到板子（需要 macOS `say`）。
