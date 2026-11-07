import asyncio
from pathlib import Path

import pytest

from bothouse_iot import app as appmod
from bothouse_iot.app import App, speakable
from bothouse_iot.config import load
from bothouse_iot.mic import Segmenter, VAD_FRAME_BYTES
import struct


def cfg(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "config.toml"
    p.write_text(f'''
[device]
listen = "127.0.0.1"
port = 0
[bothouse]
url = "ws://127.0.0.1:1/bridge/ws"
[[boards]]
device = "AA-BB-CC-DD-EE-01"
role = "ju"
project = "p"
{extra}
''')
    return p


def test_speakable_strips_footer_and_markdown():
    assert speakable("**你好**，今天\n\n*fake · 额度 · ~/x*") == "你好，今天"


def test_one_board_per_role(tmp_path):
    p = cfg(tmp_path, '[[boards]]\ndevice = "aa:bb:cc:dd:ee:02"\nrole = "ju"\nproject = "q"\n')
    with pytest.raises(ValueError):
        load(p)


def test_device_normalised(tmp_path):
    assert load(cfg(tmp_path)).boards[0].device == "aa:bb:cc:dd:ee:01"


async def test_reconnect_does_not_spam(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "BACKOFF_MIN", 0.01)
    monkeypatch.setattr(appmod, "BACKOFF_MAX", 0.02)
    lines = []
    a = App(load(cfg(tmp_path)), log=lines.append)
    t = asyncio.ensure_future(a.run())
    await asyncio.sleep(1.0)
    await a.close()
    t.cancel()
    fails = [x for x in lines if "连不上" in x]
    assert len(fails) == 1, lines


def test_segmenter_cuts_one_utterance():
    out = []
    s = Segmenter(out.append)
    loud = struct.pack("<h", 3000) * (VAD_FRAME_BYTES // 2)
    s.push(bytes(VAD_FRAME_BYTES) * 5 + loud * 30)
    s.flush()
    assert len(out) == 1 and out[0][:4] == b"RIFF"
