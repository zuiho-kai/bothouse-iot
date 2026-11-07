"""配置：一个 TOML 文件（见 config.example.toml）。设备绑定只在这一张表里：[[boards]]，一块板绑一个角色。"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .xiaozhi import norm_device


@dataclass
class Board:
    device: str                 # 板子 MAC（小智 Device-Id）
    role: str                   # 角色 id（表盘、日志、session key 用）
    project: str                # 绑定的 bothouse 项目（这个角色的 bot）
    name: str = ""              # 表盘上显示的名字
    color: str = "#9aa0a6"
    avatar: str = ""            # 头像图（相对配置文件）
    avatar_thinking: str = ""
    skins: str = ""             # 动画皮肤目录（相对配置文件），下面每个子目录一款皮肤：<皮肤>/skin.json，见 docs/dial-skins.md
    skin: str = ""              # 默认用哪款（子目录名）；不写用目录里排第一的
    key: str = ""               # 这块板自己的钥匙（可选）


@dataclass
class Config:
    path: Path
    listen: str = "0.0.0.0"
    port: int = 8791
    public_url: str = ""
    token: str = ""
    remote: bool = False        # 板子经云端转发连进来（docs/remote.md）：要 public_url，require_key 默认开
    require_key: bool = False
    firmware: str = ""
    firmware_release: str = ""  # GitHub 仓 "<owner>/<repo>"：从它的最新 Release 取固件（release.py）
    firmware_asset: str = ""
    data_dir: str = "data"
    bothouse_url: str = "ws://127.0.0.1:9810/bridge/ws"
    bothouse_token: str = ""
    platform: str = "iot"
    tts: str = "bothouse"
    tts_command: list[str] = field(default_factory=list)
    stt: str = "bothouse"
    stt_command: list[str] = field(default_factory=list)
    boards: list[Board] = field(default_factory=list)

    def rel(self, p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else self.path.parent / q

    def by_role(self) -> dict[str, Board]:
        return {b.role: b for b in self.boards}


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    d = tomllib.loads(path.read_text())
    dev, bh, sp = d.get("device", {}), d.get("bothouse", {}), d.get("speech", {})
    remote = bool(dev.get("remote", False))
    c = Config(
        path=path,
        listen=dev.get("listen", "0.0.0.0"), port=int(dev.get("port", 8791)), public_url=dev.get("public_url", ""),
        token=os.environ.get("BOTHOUSE_IOT_TOKEN", dev.get("token", "")), remote=remote,
        require_key=bool(dev.get("require_key", remote)),
        firmware=dev.get("firmware", ""), firmware_release=dev.get("firmware_release", ""),
        firmware_asset=dev.get("firmware_asset", ""), data_dir=dev.get("data_dir", "data"),
        bothouse_url=bh.get("url", "ws://127.0.0.1:9810/bridge/ws"),
        bothouse_token=os.environ.get("BOTHOUSE_BRIDGE_TOKEN", bh.get("token", "")),
        platform=bh.get("platform", "iot"),
        tts=sp.get("tts", "bothouse"), tts_command=list(sp.get("tts_command", [])),
        stt=sp.get("stt", "bothouse"), stt_command=list(sp.get("stt_command", [])),
    )
    seen: set[str] = set()
    for b in d.get("boards", []):
        board = Board(device=norm_device(b["device"]), role=b["role"], project=b["project"], name=b.get("name", b["role"]),
                      color=b.get("color", "#9aa0a6"), avatar=b.get("avatar", ""),
                      avatar_thinking=b.get("avatar_thinking", ""), skins=b.get("skins", ""), skin=b.get("skin", ""),
                      key=b.get("key", ""))
        if board.role in seen:
            raise ValueError(f"角色 {board.role} 绑了两块板子：一块板绑一个角色")
        seen.add(board.role)
        c.boards.append(board)
    if c.remote and not c.public_url:
        raise ValueError("device.remote = true 要写 public_url（云端转发的对外地址，见 docs/remote.md）")
    if c.firmware and c.firmware_release:
        raise ValueError("device.firmware 和 device.firmware_release 只能写一个")
    if c.tts == "command" and not c.tts_command:
        raise ValueError("speech.tts = \"command\" 要写 tts_command")
    if c.stt == "command" and not c.stt_command:
        raise ValueError("speech.stt = \"command\" 要写 stt_command")
    return c
