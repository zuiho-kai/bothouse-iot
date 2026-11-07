"""本地语音合成 / 转写命令（可选）。默认都交给 bothouse 的 [tts] / [speech]，这里只在配置成 "command" 时用。

命令写成参数列表，占位符：{text} 要合成的文字；{in} 输入音频文件（WAV 16k 单声道）；{out} 输出文件路径。
合成命令把音频写进 {out}（任何 ffmpeg 读得懂的格式）；转写命令把文字打印到标准输出。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

TIMEOUT_SEC = 60


def _fill(cmd: list[str], **kw: str) -> list[str]:
    return [a.format(**kw) for a in cmd]


def synth(cmd: list[str], text: str, suffix: str = ".aiff") -> bytes:
    with tempfile.TemporaryDirectory(prefix="bhiot-tts-") as d:
        out = Path(d) / f"out{suffix}"
        subprocess.run(_fill(cmd, text=text, out=str(out), **{"in": ""}), check=True, capture_output=True, timeout=TIMEOUT_SEC)
        return out.read_bytes()


def transcribe(cmd: list[str], wav: bytes) -> str:
    with tempfile.TemporaryDirectory(prefix="bhiot-stt-") as d:
        src = Path(d) / "in.wav"
        src.write_bytes(wav)
        p = subprocess.run(_fill(cmd, text="", out="", **{"in": str(src)}), check=True, capture_output=True,
                           timeout=TIMEOUT_SEC)
        return p.stdout.decode("utf-8", errors="replace").strip()
