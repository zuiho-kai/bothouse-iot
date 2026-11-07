# 出门也能用：板子经云端转发连回家里的设备服务

板子离开家里 Wi-Fi（比如连手机热点）时，局域网地址就连不上了。做法是让板子连一台有公网地址的服务器，
服务器把请求原样转回家里跑着的设备服务。设备服务、bothouse 都留在家里，云端只转发，不存任何东西。

```
板子 ──https POST <对外地址>/xiaozhi/ota/──▶ 云服务器 nginx 443 ──▶ 127.0.0.1:18791 ══ssh -R 反向隧道══▶ 家里 127.0.0.1:8791（设备服务）
板子 ◀── {"websocket":{"url":"wss://<对外地址>/xiaozhi/v1/"}} ──（设备服务配了 public_url，回云端地址）
板子 ══wss <对外地址>/xiaozhi/v1/══▶ nginx ══▶ 隧道 ══▶ 设备服务（同一条常连 WebSocket：推语音、传麦克风、表盘）
```

- 家里不用开端口、不用公网 IP：家里的机器主动 `ssh -R` 连到云服务器，隧道断了自动重连。
- 固件不用改、不用重编：板子先读 NVS 里的 `wifi:ota_url`，没有才用编进去的地址。换服务器只改 NVS。
- 小智固件原生支持记多个 Wi-Fi，开机连搜得到的那个：家里 Wi-Fi 和手机热点都写进去，在家出门都能连。
- 在家也走云端，只维护一条路。云端或隧道挂了可以 `--ota-url ""` 退回局域网直连。

## 鉴权：每块板一把钥匙

对公网开放后，谁都能往你的地址发请求，所以远程模式下默认只放行配了钥匙的板子：

- 设备服务配置 `[device] remote = true` 时，`require_key` 默认打开（显式写 `require_key = false` 才关）。
- 没配钥匙、或没绑定的板子：OTA 回 403，WebSocket 回 401。钥匙不对：WebSocket 回 401。
- 钥匙用 `tools/board-nvs.py --ws-token-gen` 生成并写进板子 NVS（`websocket:token`），同一串写进这块板的 `[[boards]].key`。
  OTA 回包对有钥匙的板子不带 token，板子 NVS 里的钥匙不会被覆盖。
- 对外地址里再加一段随机路径（下面的 `<随机前缀>`），扫端口的扫不到入口。它只是第二道门，主要靠钥匙。

不开远程模式时（默认）一切照旧：局域网直连，钥匙可选，老板子不用改。

## 部署步骤

下面 `example.com` 换成你云服务器的域名或 IP，`<随机前缀>` 换成一段随机字符（`openssl rand -hex 12`），`cloud` 是你 `~/.ssh/config` 里这台服务器的名字。

### 1. 设备服务配置（家里）

```toml
[device]
listen = "127.0.0.1"          # 只让隧道连；局域网还要直连就留 0.0.0.0
port = 8791
remote = true
public_url = "https://example.com/<随机前缀>"

[[boards]]
device = "aa:bb:cc:dd:ee:01"
role = "ju"
project = "my-project"
key = "<这块板的钥匙>"        # tools/board-nvs.py --ws-token-gen 打印出来的
```

### 2. 云服务器 nginx

把 [deploy/remote/nginx-location.conf.example](../deploy/remote/nginx-location.conf.example) 里的 `location` 段加进已有的 443 `server`，
替换 `<随机前缀>`，然后 `nginx -t && systemctl reload nginx`。改之前先备份原配置。

### 3. 反向隧道（家里）

macOS 用 launchd：把 [deploy/remote/bothouse-iot-tunnel.plist.example](../deploy/remote/bothouse-iot-tunnel.plist.example) 改好服务器名，
复制到 `~/Library/LaunchAgents/` 后 `launchctl bootstrap gui/$(id -u) <plist>`。
Linux 用 systemd：[deploy/remote/bothouse-iot-tunnel.service.example](../deploy/remote/bothouse-iot-tunnel.service.example)。

两者都是同一条命令：`ssh -N -R 127.0.0.1:18791:127.0.0.1:8791 cloud`（15 秒保活、3 次没回就断开重连）。

### 4. 每块板子写地址和热点（USB 连上，板子会复位一次）

```bash
python tools/board-nvs.py --port /dev/cu.usbmodemXXXX --ota-url "https://example.com/<随机前缀>/xiaozhi/ota/"
python tools/board-nvs.py --port /dev/cu.usbmodemXXXX --wifi "手机热点名"     # 密码在提示里输，不回显、不落盘
python tools/board-nvs.py --port /dev/cu.usbmodemXXXX --ws-token-gen          # 生成钥匙，抄进 [[boards]].key
```

只看板子 NVS（密码显示 `<略>`）：`--show`。

## 出门测试

1. 手机开热点，板子换充电宝带出门（离开家里 Wi-Fi）。
2. 板子开机连上热点，屏幕回到角色表盘。
3. 对板子说话，角色在板子上回话。家里的机器要开着，设备服务和隧道在跑。
4. 不通时看设备服务日志：有没有 `<角色> 的板子 … 连上了（… 经 https://example.com/…）`；
   有 `钥匙不对或没配，拒绝` 就是钥匙没写对。

## 网页表盘

远程模式下网页表盘也在同一个地址：`https://example.com/<随机前缀>/dial`。它只显示角色名字和状态，不能操作。
