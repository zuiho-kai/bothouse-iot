"""圆屏表盘：告诉板子「你是谁、现在在干什么」，并按板子屏幕大小给头像图。抽自 role-household rh/dial.py。

    服务 -> 板子（小智 WebSocket 上的一条 JSON；板子连上时发全量，之后状态变了再发）：
    {"type": "rh_dial", "role": "ju", "name": "橘", "color": "#f0a020", "live": true, "state": "idle",
     "avatar": "http://<服务>/xiaozhi/dial/ju.rgb565", "avatar_think": "http://<服务>/xiaozhi/dial/ju-thinking.rgb565",
     "anim": "http://<服务>/xiaozhi/dial/ju.anim"}      （anim 只在默认皮肤录了圆屏帧时有：板子 GET ?size=<边长> 取帧包）
    state：idle / thinking / speaking。板子 GET 头像地址并加 ?size=<画布边长>，服务回 size×size 的 RGB565 小端裸像素。
    固件侧：firmware/ 补丁 main/display/rh_dial_display.cc + components/cn_dial*。

网页表盘（没有板子也能看角色状态，抽自 role-household rh/dial.py 的 /rh/dial/events 和 rh/static/dial.html）：
    GET /dial          圆屏页面（static/dial.html）；/dial?role=ju 只显示一个
    GET /dial/events   SSE：连上先给每个角色一条全量，之后谁的状态变了推谁；板子上下线也推
    {"type": "rh_dial", "role": "ju", "name": "橘", "color": "#f0a020", "state": "idle", "board": true,
     "avatar": "dial/img/ju.jpg", "avatar_think": "dial/img/ju-thinking.jpg"}     （图片地址相对页面，过反向代理前缀也能用）
    GET /dial/img/<角色>[-thinking].jpg  头像原图（浏览器自己缩放）
    GET /dial/skin/<角色>/<皮肤>/<文件>  会动的表盘的皮肤（配置 skins 目录下，docs/dial-skins.md）；
        消息里多 "skin": "dial/skin/ju/pastel/"（默认皮肤）、"skins": ["pastel", "standard"]；页面 ?skin= 可换
    正在回答时消息带这句话的音量包络 "env": [0..1, ...]（每格 ENV_MS 毫秒），网页按它开合嘴
"""
from __future__ import annotations

import array
import asyncio
import json
import subprocess
import threading
from pathlib import Path
from typing import Callable

from aiohttp import web

from .config import Config
from .xiaozhi import XiaozhiServer, find_ffmpeg

MSG_TYPE = "rh_dial"
IMAGE_PATH = "/xiaozhi/dial/"
MAX_SIZE, MIN_SIZE = 1024, 16
STATES = ("idle", "thinking", "speaking")
PAGE_PATH, EVENTS_PATH, PAGE_IMAGE_PATH = "/dial", "/dial/events", "/dial/img/"
SKIN_PATH, STATIC_PATH = "/dial/skin/", "/dial/static/"
ENV_MS = 50                   # 音量包络每格毫秒（来源：实测中文合成约 3.6 字/秒，每字约 5 格，够分出字间停顿）
ENV_MAX = 3600                # 包络最多格数 = 180 秒（来源：沿用 app.py PLAY_TIMEOUT_SEC，一句最长就放这么久）
SSE_KEEPALIVE = 30.0          # SSE 空闲时多久发一行注释保活（秒，同 role-household）
STATIC = Path(__file__).with_name("static")


class Dial:
    def __init__(self, server: XiaozhiServer, cfg: Config, log: Callable[[str], None] = print):
        self.server, self.cfg, self.log = server, cfg, log
        self.boards = cfg.by_role()
        self.state: dict[str, str] = {r: "idle" for r in self.boards}
        self._base: dict[str, str] = {}
        self._images: dict[str, tuple[float, int, bytes]] = {}
        self._mu = threading.Lock()
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        server.routes += [("GET", IMAGE_PATH + "{name}", self._image), ("GET", PAGE_PATH, self._page),
                          ("GET", EVENTS_PATH, self._events), ("GET", PAGE_IMAGE_PATH + "{name}", self._page_image),
                          ("GET", SKIN_PATH + "{role}/{skin}/{name}", self._skin_file),
                          ("GET", STATIC_PATH + "{name:.+}", self._static)]
        self._env: dict[str, list[float]] = {}
        server.on_board = self._on_board
        prev = server.on_change
        server.on_change = lambda: (prev(), self._broadcast_all())

    def set(self, role: str, state: str, env: list[float] | None = None) -> None:
        """任何线程可调。状态没变不推（正在回答时换了一句新的音量包络照推）。"""
        if role not in self.boards or state not in STATES:
            return
        with self._mu:
            if self.state.get(role) == state and not (state == "speaking" and env):
                return
            self.state[role] = state
            if state == "speaking" and env:
                self._env[role] = env
            else:
                self._env.pop(role, None)
            base = self._base.get(role)
        if base is not None:
            self.server.push(role, {"type": MSG_TYPE, "role": role, "state": state})
        self._broadcast(self.page_message(role))

    def message(self, role: str, base: str) -> dict:
        b = self.boards[role]
        think = "-thinking" if b.avatar_thinking else ""
        m = {"type": MSG_TYPE, "role": role, "name": b.name, "color": b.color, "live": True,
             "state": self.state.get(role, "idle"), "avatar": f"{base}{IMAGE_PATH}{role}.rgb565",
             "avatar_think": f"{base}{IMAGE_PATH}{role}{think}.rgb565"}
        if self.frames(role):
            m["anim"] = f"{base}{IMAGE_PATH}{role}.anim"   # 板子加 ?size=<画布边长> 取录好的循环帧（固件 rh_dial_anim）
        return m

    def _on_board(self, role: str, base: str) -> None:
        if role not in self.boards:
            return
        with self._mu:
            self._base[role] = base
            m = self.message(role, base)
        self.server.push(role, m)
        self.log(f"[dial] 告诉 {role} 的板子：{m['name']}，{m['state']}")

    def picture(self, role: str, thinking: bool = False) -> Path | None:
        b = self.boards.get(role)
        if b is None:
            return None
        for p in ([b.avatar_thinking] if thinking else []) + [b.avatar]:
            if p and self.cfg.rel(p).is_file():
                return self.cfg.rel(p)
        return None

    def pixels(self, src: Path, size: int) -> bytes:
        """居中裁方再缩成 size×size，转 RGB565 小端裸像素（LVGL LV_COLOR_FORMAT_RGB565）。按文件修改时间缓存。"""
        key, mtime = str(src), src.stat().st_mtime
        hit = self._images.get(key)
        if hit is not None and hit[0] == mtime and hit[1] == size:
            return hit[2]
        vf = f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}:flags=lanczos"
        p = subprocess.run([find_ffmpeg(), "-loglevel", "error", "-i", str(src), "-frames:v", "1",
                            "-vf", vf, "-pix_fmt", "rgb565le", "-f", "rawvideo", "pipe:1"], capture_output=True, check=True)
        if len(p.stdout) != size * size * 2:
            raise ValueError(f"ffmpeg 输出 {len(p.stdout)} 字节，应为 {size * size * 2}")
        self._images[key] = (mtime, size, p.stdout)
        return p.stdout

    def frames(self, role: str, size: int | None = None) -> Path | None:
        """默认皮肤里录好的圆屏帧包 frames-<size>.bin（tools/dial_frames.py）；不给 size 时有任何一个就返回。"""
        skins = self.skins(role)
        if not skins:
            return None
        d = self.cfg.rel(self.boards[role].skins) / skins[0]
        if size is None:
            return next(iter(sorted(d.glob("frames-*.bin"))), None)
        f = d / f"frames-{size}.bin"
        return f if f.is_file() else None

    async def _image(self, req: web.Request) -> web.StreamResponse:
        name = req.match_info["name"]
        if name.endswith(".anim"):
            try:
                f = self.frames(name[:-len(".anim")], int(req.query.get("size", "0")))
            except ValueError:
                raise web.HTTPBadRequest(text="size 要是整数")
            if f is None:
                raise web.HTTPNotFound()   # 没录这个尺寸：板子留着静态头像
            return web.FileResponse(f)
        if not name.endswith(".rgb565"):
            raise web.HTTPNotFound()
        stem = name[:-len(".rgb565")]
        thinking = stem.endswith("-thinking")
        role = stem[:-len("-thinking")] if thinking else stem
        try:
            size = int(req.query.get("size", "240"))
        except ValueError:
            raise web.HTTPBadRequest(text="size 要是整数")
        if not MIN_SIZE <= size <= MAX_SIZE:
            raise web.HTTPBadRequest(text=f"size 要在 {MIN_SIZE}..{MAX_SIZE}")
        src = self.picture(role, thinking)
        if src is None:
            raise web.HTTPNotFound()
        try:
            data = await asyncio.get_running_loop().run_in_executor(None, self.pixels, src, size)
        except (OSError, subprocess.CalledProcessError, ValueError) as e:
            self.log(f"[dial] {role} 的头像转不出来：{e!r}")
            raise web.HTTPInternalServerError()
        return web.Response(body=data, content_type="application/octet-stream")

    # ---------- 会动的表盘：皮肤和音量包络 ----------
    def skins(self, role: str) -> list[str]:
        """这个角色有哪几款皮肤（skins 目录下带 skin.json 的子目录），默认那款排第一。"""
        b = self.boards.get(role)
        if b is None or not b.skins:
            return []
        d = self.cfg.rel(b.skins)
        names = sorted(p.parent.name for p in d.glob("*/skin.json")) if d.is_dir() else []
        if b.skin in names:
            names.remove(b.skin)
            names.insert(0, b.skin)
        return names

    @staticmethod
    def envelope(audio: bytes) -> list[float]:
        """音频 → 每 ENV_MS 毫秒一格的响度（0..1，按这句最响的那格归一），网页和板子拿它开合嘴。解不出来回空表。"""
        try:
            p = subprocess.run([find_ffmpeg(), "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
                               input=audio, capture_output=True, check=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return []
        pcm = array.array("h", p.stdout[:len(p.stdout) // 2 * 2])
        step = 8000 * ENV_MS // 1000
        rms = [(sum(x * x for x in pcm[i:i + step]) / max(1, len(pcm[i:i + step]))) ** 0.5 for i in range(0, len(pcm), step)][:ENV_MAX]
        top = max(rms, default=0)
        return [round(min(1.0, r / top), 2) for r in rms] if top > 0 else []

    async def _static(self, req: web.Request) -> web.StreamResponse:
        """动画脚本：static/dial-figure.js 和 static/vendor/ 下的引擎。只给 .js 和许可文本，不出 static 目录。"""
        f = (STATIC / req.match_info["name"]).resolve()
        if not f.is_relative_to(STATIC.resolve()) or not f.is_file() or f.suffix not in (".js", ".md", "") or f.name == "dial.html":
            raise web.HTTPNotFound()
        ctype = "text/javascript" if f.suffix == ".js" else "text/plain"
        return web.FileResponse(f, headers={"Cache-Control": "no-cache", "Content-Type": ctype + "; charset=utf-8"})

    async def _skin_file(self, req: web.Request) -> web.StreamResponse:
        role, skin, name = req.match_info["role"], req.match_info["skin"], req.match_info["name"]
        if skin not in self.skins(role) or "/" in name or name.startswith("."):
            raise web.HTTPNotFound()
        f = self.cfg.rel(self.boards[role].skins) / skin / name
        if not f.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(f, headers={"Cache-Control": "no-cache"})

    # ---------- 网页表盘（SSE） ----------
    def page_message(self, role: str) -> dict:
        b = self.boards[role]
        think = "-thinking" if b.avatar_thinking else ""
        skins = self.skins(role)
        m = {"type": MSG_TYPE, "role": role, "name": b.name, "color": b.color, "state": self.state.get(role, "idle"),
             "board": self.server.online(role), "avatar": f"{PAGE_IMAGE_PATH[1:]}{role}.jpg" if b.avatar else "",
             "avatar_think": f"{PAGE_IMAGE_PATH[1:]}{role}{think}.jpg" if b.avatar else "",
             "skins": skins, "skin_base": f"{SKIN_PATH[1:]}{role}/" if skins else ""}
        env = self._env.get(role)
        if env and m["state"] == "speaking":
            m["env"], m["env_ms"] = env, ENV_MS
        return m

    def _broadcast(self, m: dict) -> None:
        """任何线程可调：发给所有开着的网页。"""
        loop = self._loop
        if loop is None or not self._subs:
            return
        for q in list(self._subs):
            loop.call_soon_threadsafe(q.put_nowait, m)

    def _broadcast_all(self) -> None:
        for r in self.boards:
            self._broadcast(self.page_message(r))

    async def _page(self, req: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "dial.html", headers={"Cache-Control": "no-cache"})

    async def _events(self, req: web.Request) -> web.StreamResponse:
        self._loop = asyncio.get_running_loop()
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})   # nginx 反向代理时别攒着
        await resp.prepare(req)
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        me = asyncio.current_task()
        self.server._tasks.add(me)   # 服务退出时 XiaozhiServer.close 取消它，不让关服务等这条长连接
        self.log(f"[dial] 网页表盘连上 {req.remote}")
        try:
            for r in self.boards:
                await self._sse(resp, self.page_message(r))
            while True:
                try:
                    m = await asyncio.wait_for(q.get(), SSE_KEEPALIVE)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                await self._sse(resp, m)
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            self._subs.discard(q)
            self.server._tasks.discard(me)
            self.log(f"[dial] 网页表盘断开 {req.remote}")
        return resp

    @staticmethod
    async def _sse(resp: web.StreamResponse, m: dict) -> None:
        await resp.write(("data: " + json.dumps(m, ensure_ascii=False) + "\n\n").encode())

    async def _page_image(self, req: web.Request) -> web.StreamResponse:
        name = req.match_info["name"]
        if not name.endswith(".jpg"):
            raise web.HTTPNotFound()
        stem = name[:-len(".jpg")]
        thinking = stem.endswith("-thinking")
        src = self.picture(stem[:-len("-thinking")] if thinking else stem, thinking)
        if src is None:
            raise web.HTTPNotFound()
        return web.FileResponse(src)
