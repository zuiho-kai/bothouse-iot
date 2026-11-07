"""不重刷固件，经 USB 串口改小智板子 NVS：服务器地址（wifi:ota_url）、Wi-Fi 列表（wifi:ssidN / passwordN）、
这块板自己的钥匙（websocket:token，配合设备服务配置里这块板的 key，见 docs/wiring.md「鉴权」）。

固件原生就读这两样（docs/remote-board.md）：
  ota_url：xiaozhi-esp32 main/ota.cc 第 56-62 行 GetCheckVersionUrl()，NVS 有就用它，没有才用编进去的 CONFIG_OTA_URL；
  Wi-Fi：managed_components/78__esp-wifi-connect/ssid_manager.cc LoadFromNvs()，键 ssid、ssid1…ssid9 / password、password1…

做法：读分区表找 nvs 分区 → 整个分区读进内存 → 解析出所有键 → 改/加键 → 用 ESP-IDF 的 nvs_partition_gen 重新生成 → 写回 → 复位。
全程在内存里，不写临时文件；密码不打印。别的键（板子 uuid、音量、PHY 校准等）原样保留。

用 ESP-IDF 的 Python 跑（里面有 esptool 和 nvs 工具）：
  PY=~/.espressif/python_env/idf6.1_py3.12_env/bin/python
  $PY tools/board-nvs.py --port /dev/cu.usbmodem101 --show                       # 只看（密码显示成 <略>）
  $PY tools/board-nvs.py --port /dev/cu.usbmodem101 --ota-url "http://<设备服务地址>:8791/xiaozhi/ota/"
  $PY tools/board-nvs.py --port /dev/cu.usbmodem101 --ws-token-gen                # 生成一把钥匙写进板子，并打印出来抄进配置
  $PY tools/board-nvs.py --port /dev/cu.usbmodem101 --wifi "手机热点名"           # 密码在提示里输（不回显、不进 shell 历史）
可以一次同时给 --ota-url 和 --wifi。密码也可以用环境变量 BOARD_WIFI_PASSWORD 给（脚本调用时用）。
"""
from __future__ import annotations

import argparse
import getpass
import io
import os
import struct
import sys
from pathlib import Path

from esptool import cmds

IDF = Path.home() / "esp/esp-idf/components/nvs_flash"
sys.path.insert(0, str(IDF / "nvs_partition_tool"))
import nvs_parser  # noqa: E402
from esp_idf_nvs_partition_gen import nvs_partition_gen as gen  # noqa: E402

# 分区表位置和最大长度。来源：ESP-IDF 默认 CONFIG_PARTITION_TABLE_OFFSET=0x8000、gen_esp32part.py MAX_PARTITION_LENGTH=0xC00。
PT_OFFSET, PT_SIZE = 0x8000, 0xC00
MAX_WIFI = 10  # 来源：78__esp-wifi-connect/ssid_manager.cc 第 9 行 MAX_WIFI_SSID_COUNT 10
# 固件 Settings / SsidManager 用的命名空间（ota.cc 第 57 行 Settings("wifi")、ssid_manager.cc NVS_NAMESPACE "wifi"）。
NS = "wifi"
INT_ENC = {"uint8_t": "u8", "int8_t": "i8", "uint16_t": "u16", "int16_t": "i16",
           "uint32_t": "u32", "int32_t": "i32", "uint64_t": "u64", "int64_t": "i64"}


def find_nvs(pt: bytes) -> tuple[int, int]:
    """分区表每条 32 字节：magic AA 50、type、subtype、offset、size、label（gen_esp32part.py 的 <2sBBLL16sL）。
    小智 16m.csv 只有一个 nvs，32m.csv 另有 nvsfactory：取 label 叫 nvs 的。"""
    for i in range(0, len(pt), 32):
        e = pt[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        off, size = struct.unpack("<II", e[4:12])
        if e[2] == 1 and e[3] == 2 and e[12:28].rstrip(b"\0") == b"nvs":
            return off, size
    raise SystemExit("分区表里没找到 nvs 分区")


def parse(raw: bytes) -> dict[str, list[tuple[str, str, object]]]:
    """NVS 二进制 → {命名空间: [(键, 编码, 值)]}，编码用 nvs_partition_gen 的写法（u8/i32/string/binary）。"""
    part = nvs_parser.NVS_Partition("nvs", bytearray(raw))
    ns_names: dict[int, str] = {}
    entries, chunks = [], {}
    for page in part.pages:
        if page.header["status"] in ("Empty", "Erasing", "Corrupted"):
            continue
        for e in page.entries:
            if e.state != "Written" or e.is_empty or e.key is None:
                continue
            t, ns = e.metadata["type"], e.metadata["namespace"]
            if ns == 0:   # 命名空间表：键 = 名字，值 = 编号
                ns_names[e.data["value"]] = e.key
                continue
            if t in ("string", "blob", "blob_data"):
                body = b"".join(bytes(c.raw) for c in e.children)[:e.data["size"]]
                if t == "blob_data":
                    chunks[(ns, e.key, e.metadata["chunk_index"])] = body
                else:
                    entries.append((ns, e.key, t, body))
            elif t == "blob_index" or t in INT_ENC:
                entries.append((ns, e.key, t, e.data["value"]))
    out: dict[str, list] = {}
    for ns, key, t, v in entries:
        name = ns_names.get(ns)
        if name is None:
            continue
        if t == "blob_index":   # 多块 blob：按块号把 blob_data 拼起来
            size, count, start = v
            item = (key, "binary", b"".join(chunks.get((ns, key, start + i), b"") for i in range(count))[:size])
        elif t == "string":
            item = (key, "string", v.rstrip(b"\0").decode("utf-8"))
        elif t == "blob":
            item = (key, "binary", v)
        else:
            item = (key, INT_ENC[t], v)
        out.setdefault(name, []).append(item)
    return out


def build(data: dict[str, list], size: int) -> bytes:
    buf = io.BytesIO()
    # 留一页给 NVS 自己倒腾（gen.check_size 同样减一页，__exit__ 会把它补在最后）；VERSION2 = 多页 blob 格式，ESP-IDF v4 起默认
    nvs = gen.nvs_open(buf, size - gen.Page.PAGE_PARAMS["max_size"], gen.Page.VERSION2)
    for ns, items in data.items():
        gen.write_entry(nvs, ns, "namespace", "", "")
        for key, enc, v in items:
            if enc == "binary":
                gen.write_entry(nvs, key, "data", "hex2bin", v.hex())
            else:
                gen.write_entry(nvs, key, "data", enc, v if enc == "string" else str(v))
    gen.nvs_close(nvs)
    return buf.getvalue()


def setkey(items: list, key: str, enc: str, v) -> None:
    for i, (k, _, _) in enumerate(items):
        if k == key:
            items[i] = (key, enc, v)
            return
    items.append((key, enc, v))


def show(data: dict[str, list]) -> None:
    for ns, items in data.items():
        for k, enc, v in items:
            if k.startswith("password") or "token" in k:
                v = "<略>"
            elif enc == "binary":
                v = f"<{len(v)} 字节>"
            print(f"  {ns}:{k} = {v}")


def edit(data: dict[str, list], ota_url: str | None, ssid: str | None, pw: str | None, ws_token: str | None = None) -> None:
    if ws_token is not None:
        ws = data.setdefault("websocket", [])
        setkey(ws, "token", "string", ws_token)
        print("websocket:token 已写（这块板的钥匙）")
    items = data.setdefault(NS, [])
    if ota_url is not None:
        if ota_url:
            setkey(items, "ota_url", "string", ota_url)
        else:
            items[:] = [x for x in items if x[0] != "ota_url"]
        print("wifi:ota_url 已改" if ota_url else "wifi:ota_url 已删（回到固件默认地址）")
    if ssid:
        keys = {k: v for k, _, v in items}
        names = ["ssid" if i == 0 else f"ssid{i}" for i in range(MAX_WIFI)]
        slot = next((i for i, n in enumerate(names) if keys.get(n) == ssid), None)
        if slot is None:
            slot = next((i for i, n in enumerate(names) if n not in keys), None)
        if slot is None:
            raise SystemExit(f"板子上已经记了 {MAX_WIFI} 个 Wi-Fi，满了；先用板子的配网页删一个")
        sfx = "" if slot == 0 else str(slot)
        setkey(items, f"ssid{sfx}", "string", ssid)
        setkey(items, f"password{sfx}", "string", pw or "")
        print(f"Wi-Fi「{ssid}」写在 ssid{sfx}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True)
    ap.add_argument("--ota-url", help="写 wifi:ota_url（服务器地址）；给空串 \"\" = 删掉，回到固件编译时的默认地址")
    ap.add_argument("--wifi", metavar="SSID", help="加一个 Wi-Fi（已有同名的就改密码）")
    ap.add_argument("--ws-token", help="写这块板的钥匙（websocket:token），和设备服务配置里这块板的 key 一致")
    ap.add_argument("--ws-token-gen", action="store_true", help="随机生成一把钥匙写进去，并打印出来")
    ap.add_argument("--show", action="store_true", help="只读出来看，不写")
    ap.add_argument("--dry-run", metavar="NVS_BIN", help="不连板子：拿一个 NVS 备份文件演练解析和重新生成（不写任何文件）")
    a = ap.parse_args()
    if a.ws_token_gen:
        import secrets
        a.ws_token = secrets.token_urlsafe(24)
        print(f"这块板的钥匙（抄进配置 [[boards]] key）：{a.ws_token}")
    pw = None
    if a.wifi:
        pw = os.environ.get("BOARD_WIFI_PASSWORD") or getpass.getpass(f"「{a.wifi}」的密码（不回显）：")

    if a.dry_run:
        raw = Path(a.dry_run).read_bytes()
        data = parse(raw)
        edit(data, a.ota_url, a.wifi, pw, a.ws_token)
        new = build(data, len(raw))
        assert len(new) == len(raw) and parse(new) == data, "重新生成的分区读回来和预期不一样"
        show(parse(new))
        print(f"演练通过：{len(raw)} 字节分区，重新生成后读回一致")
        return

    esp = cmds.detect_chip(a.port)
    try:
        esp = esp.run_stub()
        print("板子 MAC", ":".join(f"{b:02x}" for b in esp.read_mac()))
        off, size = find_nvs(cmds.read_flash(esp, PT_OFFSET, PT_SIZE, no_progress=True))
        data = parse(cmds.read_flash(esp, off, size, no_progress=True))
        if a.show or (a.ota_url is None and not a.wifi and a.ws_token is None):
            show(data)
            return
        edit(data, a.ota_url, a.wifi, pw, a.ws_token)
        new = build(data, size)
        assert len(new) == size and parse(new) == data, "重新生成的分区读回来和预期不一样，不写"
        cmds.write_flash(esp, [(off, new)])
        print("已写入，复位板子")
    finally:
        esp.hard_reset()
        esp._port.close()


if __name__ == "__main__":
    main()
