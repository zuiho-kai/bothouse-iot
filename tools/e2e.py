"""端到端自检：设备服务 + 适配器对着一个（隔离的）bothouse 跑一圈，用假板子说一句、收回 bot 的语音。

    BOTHOUSE_BRIDGE_TOKEN=... python tools/e2e.py --bothouse ws://127.0.0.1:19810/bridge/ws --project iot-test

假板子：按小智协议 OTA → WS hello → 上传一句话的 Opus（macOS `say` 合成）。转写用本地假命令（固定输出一句话），
合成用 macOS `say`。--speech bothouse 走默认路线：先交给 bothouse，它没配 [speech]/[tts] 时自动退到这两个本机命令。通过条件：适配器注册成功、消息发到 bothouse 的项目、bot 的回话变成 Opus 帧推到假板子。
不碰真板子，不往任何聊天频道发消息（bothouse 侧应是隔离实例）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))

from bothouse_iot.app import App  # noqa: E402
from bothouse_iot.config import load  # noqa: E402
from bothouse_iot.xiaozhi import encode_opus  # noqa: E402
from fake_board import FakeXiaozhi  # noqa: E402

DEVICE = "aa:bb:cc:dd:ee:01"
HEARD = "你好，今天天气怎么样"


async def main(a) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bhiot-e2e-"))
    cfg = tmp / "config.toml"
    cfg.write_text(f'''
[device]
listen = "127.0.0.1"
port = 0
data_dir = "{tmp}/data"
[bothouse]
url = "{a.bothouse}"
platform = "iot"
[speech]
stt = "{a.speech}"
stt_command = ["/bin/echo", "{HEARD}"]
tts = "{a.speech}"
{'tts_command = ["say", "-o", "{out}", "{text}"]' if a.speech == "command" else "# 不写 tts_command：退到本机自带的 say"}
[[boards]]
device = "{DEVICE}"
role = "ju"
name = "橘"
project = "{a.project}"
key = "e2e-board-key"
''')
    app = App(load(cfg))
    runner = asyncio.ensure_future(app.run())
    await asyncio.wait_for(app.registered.wait(), 15)
    ota = f"http://127.0.0.1:{app.xz.port}/xiaozhi/ota/"

    # 每板一把钥匙：没钥匙的连不上，有钥匙的连得上
    bad = FakeXiaozhi(ota, DEVICE)
    await bad.ota()
    assert "token" not in bad.ota_reply["websocket"], "配了 key 的板子 OTA 不该回 token"
    try:
        await bad.connect()
        print("FAIL 没钥匙也连上了")
        return 1
    except Exception as e:
        print(f"OK 没钥匙的板子被拒：{type(e).__name__}")
    finally:
        await bad.close()
    board = FakeXiaozhi(ota, DEVICE)
    await board.ota()
    board.ota_reply["websocket"]["token"] = "e2e-board-key"   # 真板子的钥匙在 NVS 里（tools/board-nvs.py --ws-token）
    hello = await board.connect()
    print(f"OK 假板子连上：hello session {hello.get('session_id', '')[:8]}")
    await asyncio.sleep(0.5)

    # 板子说一句：say 合成 → Opus 60ms 帧 → 按节奏上传
    aiff = tmp / "speak.aiff"
    subprocess.run(["say", "-o", str(aiff), HEARD], check=True)
    packets = encode_opus(aiff.read_bytes())
    print(f"OK 假板子上传 {len(packets)} 帧 Opus（{len(packets) * 0.06:.1f} 秒）")
    await board.speak_into_mic(packets)

    t0 = time.monotonic()
    while time.monotonic() - t0 < a.timeout:
        if board.jsons(type="tts", state="stop") and board.audio():
            break
        await asyncio.sleep(0.2)
    subs = [d.get("text") for d in board.jsons(type="tts", state="sentence_start")]
    frames = board.audio()
    dials = [d.get("state") for d in board.jsons(type="rh_dial")]
    print(f"发给 bothouse：{[(m['project'], m['session_key'], m['content']) for m in app.sent]}")
    print(f"板子收到字幕：{subs}")
    print(f"板子收到音频帧：{len(frames)} 帧（{len(frames) * 0.06:.1f} 秒），表盘状态序列：{dials}")
    ok = bool(app.sent) and any(HEARD[:4] in (s or "") for s in subs) and len(frames) > 5
    print("E2E PASS" if ok else "E2E FAIL")
    await board.close()
    await app.close()
    runner.cancel()
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bothouse", default="ws://127.0.0.1:19810/bridge/ws")
    ap.add_argument("--project", default="iot-test")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--speech", choices=["command", "bothouse"], default="command",
                    help="bothouse：默认路线（交给 bothouse，它没配语音时退到本机命令）")
    sys.exit(asyncio.run(main(ap.parse_args())))
