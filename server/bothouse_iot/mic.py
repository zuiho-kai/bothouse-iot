"""板子收音：板子上传的麦克风 Opus 解成 16k PCM，按音量切成一句一句（WAV），交给上层（转文字后发给 bot）。

Ogg 封装和常驻 ffmpeg 解码抽自 role-household rh/board_mic.py。固件（firmware/ 补丁 CONFIG_RH_ALWAYS_LISTEN）空闲时
一直上传、板上没有 VAD，所以这里按能量切句：连续 START_MS 有声算开口，END_SILENCE_MS 静音算一句说完。
"""
from __future__ import annotations

import io
import struct
import subprocess
import threading
import time
import wave
from typing import Callable

from .xiaozhi import FRAME_MS as OPUS_FRAME_MS, SAMPLE_RATE, find_ffmpeg

OGG_SERIAL = 0x62686F74
GRANULE_PER_PACKET = 48000 * OPUS_FRAME_MS // 1000   # Ogg Opus granule 按 48k 计（RFC 7845 第 4 节）
VAD_FRAME_MS = 30
VAD_FRAME_BYTES = SAMPLE_RATE * VAD_FRAME_MS // 1000 * 2
# 切句参数。来源：实现者定；END_SILENCE_MS 同 role-household rh/voice.py（0.7 秒），RMS 阈值按 16 位 PCM 粗定。
START_RMS = 600
START_MS = 150
END_SILENCE_MS = 700
MIN_UTTER_MS = 400
MAX_UTTER_MS = 15000
PRE_ROLL_MS = 300

_CRC = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04C11DB7) if _r & 0x80000000 else (_r << 1)
    _CRC.append(_r & 0xFFFFFFFF)


def ogg_crc(data: bytes) -> int:
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _CRC[((c >> 24) ^ b) & 0xFF]
    return c


def ogg_page(packet: bytes, serial: int, seq: int, granule: int, flags: int = 0) -> bytes:
    segs = [255] * (len(packet) // 255) + [len(packet) % 255]
    head = struct.pack("<4sBBqIIIB", b"OggS", 0, flags, granule, serial, seq, 0, len(segs)) + bytes(segs)
    page = bytearray(head + packet)
    struct.pack_into("<I", page, 22, ogg_crc(bytes(page)))
    return bytes(page)


def opus_head(rate: int = SAMPLE_RATE) -> bytes:
    return struct.pack("<8sBBHIhB", b"OpusHead", 1, 1, 0, rate, 0, 0)


def opus_tags() -> bytes:
    vendor = b"bothouse-iot"
    return b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def rms(frame: bytes) -> float:
    n = len(frame) // 2
    if not n:
        return 0.0
    s = struct.unpack(f"<{n}h", frame[:n * 2])
    return (sum(x * x for x in s) / n) ** 0.5


class OpusStream:
    """常驻 ffmpeg：Opus 包拼成 Ogg 流喂进去，16k 单声道 s16le PCM 出来交给 on_pcm（读线程里调）。"""

    def __init__(self, on_pcm: Callable[[bytes], None], ffmpeg: str | None = None):
        self.on_pcm = on_pcm
        self.p = subprocess.Popen([ffmpeg or find_ffmpeg(), "-loglevel", "error", "-fflags", "nobuffer",
                                   "-probesize", "32", "-analyzeduration", "0", "-f", "ogg", "-i", "pipe:0",
                                   "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-flush_packets", "1", "pipe:1"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.granule, self.seq = 0, 2
        self._mu = threading.Lock()
        self._write(ogg_page(opus_head(), OGG_SERIAL, 0, 0, flags=0x02))
        self._write(ogg_page(opus_tags(), OGG_SERIAL, 1, 0))
        threading.Thread(target=self._read, daemon=True).start()

    def _write(self, b: bytes) -> bool:
        try:
            self.p.stdin.write(b)
            return True
        except (BrokenPipeError, ValueError, OSError):
            return False

    def feed(self, packet: bytes) -> bool:
        with self._mu:
            self.granule += GRANULE_PER_PACKET
            ok = self._write(ogg_page(packet, OGG_SERIAL, self.seq, self.granule))
            self.seq += 1
            return ok

    def _read(self) -> None:
        while True:
            try:
                b = self.p.stdout.read(4096)
            except (ValueError, OSError):
                return
            if not b:
                return
            self.on_pcm(b)

    def alive(self) -> bool:
        return self.p.poll() is None

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(2)
        except subprocess.TimeoutExpired:
            self.p.kill()


class Segmenter:
    """30 毫秒一帧喂 PCM，切出一句就调 on_utterance(wav)。线程安全（解码读线程里调 push）。"""

    def __init__(self, on_utterance: Callable[[bytes], None]):
        self.on_utterance = on_utterance
        self._mu = threading.Lock()
        self._buf = b""
        self._pre: list[bytes] = []
        self._voiced = 0
        self._silent = 0
        self._utter: list[bytes] | None = None

    def push(self, pcm: bytes) -> None:
        out = None
        with self._mu:
            self._buf += pcm
            while len(self._buf) >= VAD_FRAME_BYTES:
                f, self._buf = self._buf[:VAD_FRAME_BYTES], self._buf[VAD_FRAME_BYTES:]
                out = self._frame(f) or out
        if out:
            self.on_utterance(out)

    def _frame(self, f: bytes) -> bytes | None:
        loud = rms(f) >= START_RMS
        if self._utter is None:
            self._pre = (self._pre + [f])[-(PRE_ROLL_MS // VAD_FRAME_MS):]
            self._voiced = self._voiced + 1 if loud else 0
            if self._voiced * VAD_FRAME_MS >= START_MS:
                self._utter, self._silent = list(self._pre), 0
            return None
        self._utter.append(f)
        self._silent = 0 if loud else self._silent + 1
        ms = len(self._utter) * VAD_FRAME_MS
        if self._silent * VAD_FRAME_MS >= END_SILENCE_MS or ms >= MAX_UTTER_MS:
            u, self._utter, self._voiced, self._pre = self._utter, None, 0, []
            if ms >= MIN_UTTER_MS:
                return pcm_to_wav(b"".join(u))
        return None

    def flush(self) -> None:
        """板子不再上传（断开、停了一会儿）：补够静音让最后一句收尾。"""
        self.push(bytes(VAD_FRAME_BYTES * (END_SILENCE_MS // VAD_FRAME_MS + 1)))


class BoardMic:
    """每块（绑了角色的）板子一个解码器 + 切句器。on_packet / on_end 在事件循环里调，不阻塞。
    on_utterance(role, wav) 在解码读线程里调。"""

    IDLE_FLUSH_SEC = 1.0

    def __init__(self, on_utterance: Callable[[str, bytes], None], log: Callable[[str], None] = print,
                 decoder: Callable[[Callable[[bytes], None]], OpusStream] | None = None):
        self.on_utterance, self.log = on_utterance, log
        self.decoder = decoder or OpusStream
        self._dec: dict[str, OpusStream] = {}
        self._seg: dict[str, Segmenter] = {}
        self._last: dict[str, float] = {}
        self.muted: set[str] = set()   # 板子正在放音时不收（固件没 AEC 时本来也不传，这里再挡一层）
        self.packets = 0
        self._stop = threading.Event()
        threading.Thread(target=self._idle_loop, daemon=True).start()

    def on_packet(self, role: str, packet: bytes) -> None:
        if not packet or role in self.muted:
            return
        dec = self._dec.get(role)
        if dec is None or not dec.alive():
            seg = self._seg.setdefault(role, Segmenter(lambda wav, r=role: self.on_utterance(r, wav)))
            dec = self._dec[role] = self.decoder(seg.push)
            self.log(f"[mic] 开始收 {role} 的板子的麦克风")
        self._last[role] = time.monotonic()
        self.packets += 1
        dec.feed(packet)

    def on_end(self, role: str) -> None:
        dec = self._dec.pop(role, None)
        self._last.pop(role, None)
        if dec is not None:
            threading.Thread(target=self._retire, args=(role, dec), daemon=True).start()

    def _retire(self, role: str, dec: OpusStream) -> None:
        dec.close()
        seg = self._seg.get(role)
        if seg is not None:
            seg.flush()

    def _idle_loop(self) -> None:
        while not self._stop.wait(0.25):
            now = time.monotonic()
            for role, t in list(self._last.items()):
                if now - t >= self.IDLE_FLUSH_SEC:
                    self._last.pop(role, None)
                    seg = self._seg.get(role)
                    if seg is not None:
                        seg.flush()

    def close(self) -> None:
        self._stop.set()
        for role in list(self._dec):
            dec = self._dec.pop(role)
            dec.close()
