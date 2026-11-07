"""设备服务 + bothouse 适配器：把板子接进 bothouse，当一个能收发文字和音频的外部平台。

    板子 ──小智协议(OTA+WS)──▶ XiaozhiServer ──▶ App ──bridge-protocol WS──▶ bothouse ──▶ 绑定项目的 bot
    板子说话：麦克风 Opus → BoardMic 切句 → 转文字（交 bothouse [speech]，或本地 stt_command）→ message
    bot 回话：reply 文本 / audio → 合成（bothouse [tts] 发来的 audio，或本地 tts_command）→ Opus → 对应板子

协议：bothouse bridge/docs/bridge-protocol.zh-CN.md（register / message / reply / audio / typing / ping）。
一块板绑一个角色、一个 bothouse 项目（配置 [[boards]]），消息里带 project 字段路由到那个项目。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from collections import defaultdict
from typing import Callable

import aiohttp

from . import speech
from .config import Config
from .dial import Dial
from .mic import BoardMic
from .xiaozhi import XiaozhiServer

CAPABILITIES = ["text", "audio", "typing"]
PING_SEC = 25.0               # 协议要求至少 30 秒一次
BACKOFF_MIN, BACKOFF_MAX = 1.0, 60.0   # 协议「断线重连」：起始 1 秒，最大 60 秒
QUIET_LOG_SEC = 600.0         # 一直连不上时，多久才再记一行（不刷屏）
PLAY_TIMEOUT_SEC = 180.0
NOTICE_PREFIXES = ("🎙",)     # bothouse 转写时发的提示（正在转写 / 转写结果），不念出来
_FOOTER = re.compile(r"\n\s*\n\s*\*[^*\n]+\*\s*$")   # bothouse 回复末尾的状态脚注（*模型 · 额度 · 目录*）
_MD = re.compile(r"[*_`#>]+")


def speakable(text: str) -> str:
    """念给板子的文字：去掉 bothouse 的状态脚注和 Markdown 记号。"""
    return _MD.sub("", _FOOTER.sub("", text)).strip()


class App:
    def __init__(self, cfg: Config, log: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.log = log or (lambda s: print(time.strftime("%H:%M:%S ") + s, flush=True))
        self.boards = cfg.by_role()
        self.xz = XiaozhiServer({b.device: b.role for b in cfg.boards}, token=cfg.token, port=cfg.port, host=cfg.listen,
                                keys={b.device: b.key for b in cfg.boards if b.key}, require_key=cfg.require_key,
                                public_url=cfg.public_url, on_receipt=self._on_receipt, log=self.log)
        if cfg.firmware:
            self.xz.firmware = cfg.rel(cfg.firmware)
            self.xz.fw_asked_file = cfg.rel(cfg.data_dir) / "firmware-asked.txt"
        self.dial = Dial(self.xz, cfg, self.log)
        self.mic = BoardMic(self._on_utterance, self.log)
        self.xz.on_mic, self.xz.on_mic_end = self.mic.on_packet, self.mic.on_end
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.registered = asyncio.Event()
        self._receipts: dict[str, asyncio.Future] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._subtitle: dict[str, str] = defaultdict(str)
        self._busy: set[str] = set()      # 正在合成或播放的角色（表盘不在这期间回待机）
        self._stop = asyncio.Event()
        self.sent: list[dict] = []        # 发给 bothouse 的消息（测试、排错用）

    # ---------- 起停 ----------
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        await self.xz.start()
        for role in self.boards:
            self._queues[role] = asyncio.Queue()
            asyncio.ensure_future(self._speaker(role))
        await self._bothouse_loop()

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        if self.ws is not None:
            await self.ws.close()
        self.mic.close()
        await self.xz.close()

    # ---------- bothouse 连接 ----------
    async def _bothouse_loop(self) -> None:
        backoff, fails, last_log = BACKOFF_MIN, 0, 0.0
        headers = {"Authorization": f"Bearer {self.cfg.bothouse_token}"} if self.cfg.bothouse_token else {}
        async with aiohttp.ClientSession() as http:
            while not self._stop.is_set():
                try:
                    async with http.ws_connect(self.cfg.bothouse_url, headers=headers, heartbeat=None) as ws:
                        self.ws = ws
                        await ws.send_json({"type": "register", "platform": self.cfg.platform, "capabilities": CAPABILITIES,
                                            "metadata": {"description": "bothouse-iot device service",
                                                         "boards": [b.role for b in self.cfg.boards]}})
                        if fails:
                            self.log(f"[bothouse] 重新连上（之前连不上 {fails} 次）")
                        backoff, fails = BACKOFF_MIN, 0
                        await self._session(ws)
                    if not self._stop.is_set():
                        self.log("[bothouse] 连接断开，稍后重连")
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                    fails += 1
                    now = time.monotonic()
                    if fails == 1 or now - last_log >= QUIET_LOG_SEC:   # 第一次和之后每 10 分钟记一行
                        self.log(f"[bothouse] 连不上 {self.cfg.bothouse_url}：{e!r}（第 {fails} 次，按 1→60 秒退避重试，"
                                 f"{int(QUIET_LOG_SEC // 60)} 分钟内不再重复记）")
                        last_log = now
                finally:
                    self.ws = None
                    self.registered.clear()
                if self._stop.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop.wait(), backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(BACKOFF_MAX, backoff * 2)

    async def _session(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        pinger = asyncio.ensure_future(self._ping(ws))
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                try:
                    d = json.loads(msg.data)
                except ValueError:
                    continue
                await self._from_bothouse(d)
        finally:
            pinger.cancel()

    async def _ping(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not ws.closed:
            await asyncio.sleep(PING_SEC)
            await ws.send_json({"type": "ping", "ts": int(time.time() * 1000)})

    async def _from_bothouse(self, d: dict) -> None:
        t = d.get("type")
        if t == "register_ack":
            if d.get("ok"):
                self.registered.set()
                self.log(f"[bothouse] 注册成功：平台 {self.cfg.platform}，能力 {','.join(CAPABILITIES)}，"
                         f"板子 {', '.join(f'{b.role}→{b.project}' for b in self.cfg.boards) or '无'}")
            else:
                self.log(f"[bothouse] 注册被拒：{d.get('error')}")
            return
        if t == "pong":
            return
        role = self._role_of(d)
        if t == "reply":
            raw = str(d.get("content") or "")
            self.log(f"[bothouse] → {role}：{raw[:80]!r}")
            if role is None or raw.startswith(NOTICE_PREFIXES):
                return
            text = speakable(raw)
            if not text:
                return
            if self.cfg.tts == "command":
                self._queues[role].put_nowait((text, None))
            else:   # 声音等 bothouse [tts] 发来的 audio，文字先记下当字幕
                self._subtitle[role] = text
        elif t == "audio":
            if role is None:
                return
            data = base64.b64decode(d.get("data") or "")
            self.log(f"[bothouse] → {role}：音频 {len(data)} 字节（{d.get('format')}）")
            if self.cfg.tts != "command" and data:
                self._busy.add(role)
            self._queues[role].put_nowait((self._subtitle.pop(role, ""), data))
        elif t == "typing_start":
            if role is not None:
                self.dial.set(role, "thinking")
        elif t == "typing_stop":
            if role is not None and role not in self._busy and self._queues[role].empty():
                self.dial.set(role, "idle")
        elif t == "error":
            self.log(f"[bothouse] 错误 {d.get('code')}：{d.get('message')}")
        else:
            self.log(f"[bothouse] 收到不处理的消息 {t}")

    def _role_of(self, d: dict) -> str | None:
        ctx = d.get("reply_ctx")
        if ctx in self.boards:
            return ctx
        parts = str(d.get("session_key") or "").split(":")
        return parts[1] if len(parts) >= 3 and parts[1] in self.boards else None

    def session_key(self, role: str) -> str:
        return f"{self.cfg.platform}:{role}:{self.boards[role].device.replace(':', '')}"

    async def send_user(self, role: str, text: str = "", wav: bytes | None = None) -> bool:
        """板子这边说了一句：发给这块板绑定的项目。bothouse 没连上时丢掉并记一行。"""
        b = self.boards[role]
        m = {"type": "message", "msg_id": uuid.uuid4().hex, "session_key": self.session_key(role),
             "user_id": b.device.replace(":", ""), "user_name": f"{b.name}的板子", "content": text,
             "reply_ctx": role, "project": b.project}
        if wav is not None:
            m["audio"] = {"mime_type": "audio/wav", "format": "wav", "data": base64.b64encode(wav).decode(),
                          "duration": max(1, round((len(wav) - 44) / 32000))}
        if self.ws is None or self.ws.closed or not self.registered.is_set():
            self.log(f"[bothouse] 没连上，{role} 的板子这句丢掉")
            return False
        await self.ws.send_json(m)
        self.sent.append(m)
        self.dial.set(role, "thinking")
        self.log(f"[bothouse] ← {role}（项目 {b.project}）：{text[:80] if text else f'语音 {len(wav or b"")} 字节'}")
        return True

    # ---------- 板子收音 ----------
    def _on_utterance(self, role: str, wav: bytes) -> None:
        """解码线程里调：转到事件循环处理。"""
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self._utterance(role, wav), self.loop)

    async def _utterance(self, role: str, wav: bytes) -> None:
        self.log(f"[mic] {role} 的板子说完一句（{(len(wav) - 44) / 32000:.1f} 秒）")
        if self.cfg.stt == "command":
            try:
                text = await asyncio.get_running_loop().run_in_executor(None, speech.transcribe, self.cfg.stt_command, wav)
            except Exception as e:
                return self.log(f"[mic] 本地转写失败：{e!r}")
            if not text:
                return self.log(f"[mic] {role} 这句没转出字")
            await self.send_user(role, text=text)
        else:
            await self.send_user(role, wav=wav)

    # ---------- 放音 ----------
    def _on_receipt(self, pid: str, result: str, started=None) -> None:
        fut = self._receipts.get(pid)
        if fut is not None and not fut.done():
            fut.set_result(result)

    async def _speaker(self, role: str) -> None:
        q = self._queues[role]
        while True:
            text, audio = await q.get()
            self._busy.add(role)
            try:
                if audio is None:
                    audio = await asyncio.get_running_loop().run_in_executor(None, speech.synth, self.cfg.tts_command, text)
                await self.play(role, text, audio)
            except Exception as e:
                self.log(f"[speak] {role} 这句没播成：{e!r}")
            if q.empty():
                self._busy.discard(role)
                self.dial.set(role, "idle")

    async def play(self, role: str, text: str, audio: bytes) -> str:
        """推一段音频（任何 ffmpeg 读得懂的格式）给 role 的板子，等它播完。返回回执。"""
        pid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._receipts[pid] = fut
        self.dial.set(role, "speaking")
        self.mic.muted.add(role)
        try:
            self.xz.send([{"role": role, "play_id": pid, "event": "start", "text": text},
                          {"role": role, "play_id": pid, "event": "audio", "data": base64.b64encode(audio).decode(),
                           "last": True}])
            r = await asyncio.wait_for(fut, PLAY_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            r = "timeout"
        finally:
            self._receipts.pop(pid, None)
            self.mic.muted.discard(role)
        self.log(f"[speak] {role}：{r}（{text[:40]}）")
        return r
