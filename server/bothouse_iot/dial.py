"""圆屏表盘：告诉板子「你是谁、现在在干什么」，并按板子屏幕大小给头像图。抽自 role-household rh/dial.py。

    服务 -> 板子（小智 WebSocket 上的一条 JSON；板子连上时发全量，之后状态变了再发）：
    {"type": "rh_dial", "role": "ju", "name": "橘", "color": "#f0a020", "live": true, "state": "idle",
     "avatar": "http://<服务>/xiaozhi/dial/ju.rgb565", "avatar_think": "http://<服务>/xiaozhi/dial/ju-thinking.rgb565"}
    state：idle / thinking / speaking。板子 GET 头像地址并加 ?size=<画布边长>，服务回 size×size 的 RGB565 小端裸像素。
    固件侧：firmware/ 补丁 main/display/rh_dial_display.cc + components/cn_dial*。
"""
from __future__ import annotations

import asyncio
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


class Dial:
    def __init__(self, server: XiaozhiServer, cfg: Config, log: Callable[[str], None] = print):
        self.server, self.cfg, self.log = server, cfg, log
        self.boards = cfg.by_role()
        self.state: dict[str, str] = {r: "idle" for r in self.boards}
        self._base: dict[str, str] = {}
        self._images: dict[str, tuple[float, int, bytes]] = {}
        self._mu = threading.Lock()
        server.routes.append(("GET", IMAGE_PATH + "{name}", self._image))
        server.on_board = self._on_board

    def set(self, role: str, state: str) -> None:
        """任何线程可调。状态没变不推。"""
        if role not in self.boards or state not in STATES:
            return
        with self._mu:
            if self.state.get(role) == state:
                return
            self.state[role] = state
            base = self._base.get(role)
        if base is not None:
            self.server.push(role, {"type": MSG_TYPE, "role": role, "state": state})

    def message(self, role: str, base: str) -> dict:
        b = self.boards[role]
        think = "-thinking" if b.avatar_thinking else ""
        return {"type": MSG_TYPE, "role": role, "name": b.name, "color": b.color, "live": True,
                "state": self.state.get(role, "idle"), "avatar": f"{base}{IMAGE_PATH}{role}.rgb565",
                "avatar_think": f"{base}{IMAGE_PATH}{role}{think}.rgb565"}

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

    async def _image(self, req: web.Request) -> web.StreamResponse:
        name = req.match_info["name"]
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
