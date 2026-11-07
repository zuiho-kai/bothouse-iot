"""没有板子、没有 bothouse，也能看会动的表盘：只起设备服务的表盘部分，轮流切 待机 → 思考中 → 正在回答。

    python tools/dial_demo.py [--port 8799] [--config config.toml] [--hold idle|thinking|speaking]

不给 --config 就用仓里自带的橘（skins/ju）。打开 http://127.0.0.1:<port>/dial?role=ju（?skin=standard 换皮肤）。
正在回答那段用本机 say / espeak 合成一句话算音量包络（没有就按合成节奏动）。不连 bothouse、不碰板子、不写数据目录以外的东西。
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from bothouse_iot.config import load  # noqa: E402
from bothouse_iot.dial import Dial  # noqa: E402
from bothouse_iot.xiaozhi import XiaozhiServer  # noqa: E402

LINE = "你好呀，我是橘。今天想看哪本书？我帮你找找。"
# 每个状态停几秒（来源：交接验收「10 秒录屏里三种状态各出现」）
CYCLE = [("idle", 3.5), ("thinking", 3.0), ("speaking", 4.0)]


def speech_env(dial: Dial) -> list[float]:
    tmp = Path(tempfile.mkdtemp(prefix="dial-demo-"))
    out = tmp / "line.aiff"
    if shutil.which("say"):
        cmd = ["say", "-o", str(out), LINE]
    elif shutil.which("espeak-ng") or shutil.which("espeak"):
        out = tmp / "line.wav"
        cmd = [shutil.which("espeak-ng") or "espeak", "-v", "zh", "-w", str(out), LINE]
    else:
        return []
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        return dial.envelope(out.read_bytes())
    except (OSError, subprocess.SubprocessError):
        return []


async def main(a) -> None:
    if a.config:
        cfg = load(a.config)
    else:
        tmp = Path(tempfile.mkdtemp(prefix="dial-demo-"))
        (tmp / "config.toml").write_text(f'''
[device]
listen = "127.0.0.1"
port = {a.port}
data_dir = "{tmp}/data"
[[boards]]
device = "aa:bb:cc:dd:ee:01"
role = "ju"
name = "橘"
project = "demo"
color = "#f0a020"
avatar = "{ROOT}/skins/ju/standard/base.png"
skins = "{ROOT}/skins/ju"
skin = "{a.skin}"
''')
        cfg = load(tmp / "config.toml")
    cfg.port = a.port
    xz = XiaozhiServer({b.device: b.role for b in cfg.boards}, port=cfg.port, host=cfg.listen)
    dial = Dial(xz, cfg)
    await xz.start()
    role = cfg.boards[0].role
    print(f"打开 http://127.0.0.1:{xz.port}/dial?role={role}", flush=True)
    env = await asyncio.get_running_loop().run_in_executor(None, speech_env, dial)
    if a.hold:
        dial.set(role, a.hold, env if a.hold == "speaking" else None)
        await asyncio.Event().wait()
    while True:
        for state, sec in CYCLE:
            dial.set(role, state, env if state == "speaking" else None)
            await asyncio.sleep(sec)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8799)
    p.add_argument("--config")
    p.add_argument("--skin", default="standard")
    p.add_argument("--hold", choices=["idle", "thinking", "speaking"])
    asyncio.run(main(p.parse_args()))
