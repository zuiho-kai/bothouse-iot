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


def test_segmenter_reports_only_voice_onset():
    out, starts = [], []
    s = Segmenter(out.append, lambda: starts.append("start"))
    loud = struct.pack("<h", 3000) * (VAD_FRAME_BYTES // 2)
    s.push(loud * 30)
    s.push(loud * 10)
    assert starts == ["start"]
    s.flush()
    assert len(out) == 1


async def test_playbacks_are_serial_and_voice_start_stops_current(tmp_path, monkeypatch):
    a = App(load(cfg(tmp_path)), log=lambda s: None)
    a._queues = {"ju": asyncio.Queue(), "jingyu": asyncio.Queue()}
    a._queues["ju"].put_nowait(("one", b""))
    a._queues["jingyu"].put_nowait(("two", b""))
    sent = []
    stopped = []
    second_started = asyncio.Event()
    monkeypatch.setattr(a.dial, "envelope", lambda audio: [])
    monkeypatch.setattr(a.dial, "set", lambda *args: None)
    def send(frames):
        sent.extend(frames)
        if any(f["role"] == "jingyu" and f["event"] == "start" for f in frames):
            second_started.set()
        return True
    monkeypatch.setattr(a.xz, "send", send)
    monkeypatch.setattr(a.xz, "stop_all", lambda: stopped.append(True))
    first = asyncio.create_task(a.play("ju", "one", b"audio"))
    await asyncio.sleep(0)
    second = asyncio.create_task(a.play("jingyu", "two", b"audio"))
    await asyncio.sleep(0)
    assert [f["role"] for f in sent if f["event"] == "start"] == ["ju"]
    a._on_receipt(a._playing[1], "done")
    assert await first == "done"
    await asyncio.wait_for(second_started.wait(), 1)
    assert [f["role"] for f in sent if f["event"] == "start"] == ["ju", "jingyu"]
    a._interrupt_playback()
    assert stopped == [True]
    a._on_receipt(a._playing[1], "stopped")
    assert await second == "stopped"


async def test_tts_falls_back_to_local_when_bothouse_sends_no_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "AUDIO_WAIT_SEC", 0.05)
    a = App(load(cfg(tmp_path)), log=lambda s: None)
    a.cfg.tts_command = ["fake-tts"]
    a._queues["ju"] = asyncio.Queue()
    await a._from_bothouse({"type": "reply", "reply_ctx": "ju", "content": "你好"})
    assert a._queues["ju"].empty()          # 先等 bothouse 的音频
    await asyncio.sleep(0.1)
    assert a._queues["ju"].get_nowait() == ("你好", None)   # 等不到：本机合成
    assert a._local_tts
    await a._from_bothouse({"type": "reply", "reply_ctx": "ju", "content": "第二句"})
    assert a._queues["ju"].get_nowait() == ("第二句", None)  # 之后直接本机合成


async def test_tts_uses_bothouse_audio_when_it_arrives(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "AUDIO_WAIT_SEC", 0.05)
    a = App(load(cfg(tmp_path)), log=lambda s: None)
    a._queues["ju"] = asyncio.Queue()
    await a._from_bothouse({"type": "reply", "reply_ctx": "ju", "content": "你好"})
    await a._from_bothouse({"type": "audio", "reply_ctx": "ju", "data": "AAAA", "format": "mp3"})
    await asyncio.sleep(0.1)
    assert a._queues["ju"].get_nowait() == ("你好", b"\x00\x00\x00")
    assert a._queues["ju"].empty() and not a._local_tts


async def test_stt_falls_back_when_bothouse_has_no_speech(tmp_path):
    a = App(load(cfg(tmp_path)), log=lambda s: None)
    a.cfg.stt_command = ["fake-stt"]
    seen = []

    async def fake_utt(role, wav):
        seen.append((role, wav, a._local_stt))
    a._last_wav["ju"] = b"wav"
    a._utterance = fake_utt
    await a._from_bothouse({"type": "reply", "reply_ctx": "ju",
                            "content": "🎙 语音消息未启用，请在 config.toml 中配置 `[speech]` 部分。"})
    assert seen == [("ju", b"wav", True)]
