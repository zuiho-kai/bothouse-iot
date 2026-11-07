# 固件：xiaozhi-esp32 v2.5.2 + bothouse-iot 补丁

- `patches/`：对上游 [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) v2.5.2 的改动（一个补丁）。
  主机随时推语音（`CONFIG_RH_HOST_PUSH`）、静音本地提示音、空闲时把麦克风传给设备服务（`CONFIG_RH_ALWAYS_LISTEN`）、
  圆屏角色表盘（`CONFIG_RH_DIAL`，spotpear sp-esp32-s3-1.28-box）、hello 上报屏幕、Waveshare 1.85C 的六个触摸键。
- `components/cn_dial`、`components/cn_dial_lvgl`：表盘的屏幕尺寸计算和叠加层（名牌、状态、电量弧、气泡）。
- `scripts/build.sh`：拉干净的 v2.5.2 → 打补丁 → 放组件 → 编 OTA 地址 → 编译。
- 上游许可：`LICENSE-xiaozhi-esp32`（MIT）。

## 编译

```bash
. $IDF_PATH/export.sh                         # ESP-IDF v6.1
OTA_URL=http://<设备服务 IP>:8791/xiaozhi/ota/ scripts/build.sh [板型] [工作目录]
```

默认板型 `spotpear/sp-esp32-s3-1.28-box`，工作目录 `firmware/.build/xiaozhi-esp32`，产物和 `SHA256SUMS` 在 `firmware/.build/out/`。

## 刷写

先按 MAC 认板，没备份过的板先整片备份（16MB 约 60 秒），备份没成功不要刷：

```bash
PORT=/dev/cu.usbmodemXXX
python -m esptool -p $PORT read-mac
python -m esptool -p $PORT -b 921600 read-flash 0 0x1000000 backup-<MAC>.bin
```

**只写 otadata 和 app，不要从 0x0 写 merged 整包**：merged 包的 nvs 段（0x9000–0xD000，存 Wi-Fi）是 0xFF，会把 Wi-Fi 清掉。

```bash
python -m esptool -p $PORT -b 921600 read-flash 0x9000 0x4000 nvs-backup.bin     # 先存一份 nvs
python -m esptool --chip esp32s3 -p $PORT -b 921600 write-flash 0xd000 out/ota_data_initial.bin 0x20000 out/xiaozhi.bin
```

全新的板（分区表或 assets 要换）才写 merged 整包，写完 Wi-Fi 要重配。分区：nvs 0x9000、ota_0 0x20000、ota_1 0x410000、assets 0x800000。

刷不进时按住 BOOT 再插 USB 进下载模式，或把 `-b 921600` 降到 `460800`。

USB 不方便时可以走 Wi-Fi 升级：设备服务配置 `[device] firmware = "<xiaozhi.bin>"`，板子连上时服务比对 elf_sha256，
不一样就让它自己下载、写另一个 OTA 分区、重启（不碰 nvs；新固件起不来 bootloader 会回滚）。

## 联网和地址

板子开热点时手机连上开 http://192.168.4.1 配 Wi-Fi；「高级选项」里可改 OTA 地址（存进 nvs `ota_url`，覆盖编译默认值）。
也可以用 USB：`tools/board-nvs.py --port $PORT --ota-url ... --wifi <SSID>`；每块板的钥匙用 `--ws-token-gen`（见 docs/wiring.md「鉴权」）。

开机后屏幕是角色表盘，显示「等待主机」；连上设备服务后换成绑定角色的名字和头像。

看开机日志：`idf.py -p $PORT monitor`（在 xiaozhi-esp32 目录下，Ctrl+] 退出）。

## 恢复出厂

```bash
python -m esptool --chip esp32s3 -p $PORT -b 921600 write-flash 0 backup-<该板MAC>.bin
```

必须用这块板自己的备份。只清 nvs：`python -m esptool -p $PORT erase-region 0x9000 0x4000`（Wi-Fi 要重配）。
