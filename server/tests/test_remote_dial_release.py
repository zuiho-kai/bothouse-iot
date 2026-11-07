"""出门（远程模式 + 每板钥匙）、网页表盘 SSE、从 Release 取固件。"""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from fake_board import FakeXiaozhi  # noqa: E402

from bothouse_iot.app import App  # noqa: E402
from bothouse_iot.config import load  # noqa: E402
from bothouse_iot.release import ReleaseFirmware  # noqa: E402

DEV = "aa:bb:cc:dd:ee:01"


def cfg(tmp_path: Path, device: str = "", board: str = "") -> Path:
    p = tmp_path / "config.toml"
    p.write_text(f'''
[device]
listen = "127.0.0.1"
port = 0
data_dir = "{tmp_path}/data"
{device}
[bothouse]
url = "ws://127.0.0.1:1/bridge/ws"
[[boards]]
device = "{DEV}"
role = "ju"
name = "橘"
project = "p"
{board}
''')
    return p


def test_remote_turns_on_require_key(tmp_path):
    c = load(cfg(tmp_path, 'remote = true\npublic_url = "https://example.com/iot"'))
    assert c.remote and c.require_key


def test_remote_respects_explicit_require_key(tmp_path):
    c = load(cfg(tmp_path, 'remote = true\nrequire_key = false\npublic_url = "https://example.com/iot"'))
    assert not c.require_key


def test_remote_needs_public_url(tmp_path):
    with pytest.raises(ValueError):
        load(cfg(tmp_path, "remote = true"))


def test_default_keeps_old_boards_working(tmp_path):
    c = load(cfg(tmp_path))
    assert not c.remote and not c.require_key


async def started(tmp_path, device="", board="") -> App:
    a = App(load(cfg(tmp_path, device, board)), log=lambda s: None)
    a.loop = asyncio.get_running_loop()
    await a.xz.start()
    return a


async def test_remote_wrong_key_rejected_right_key_connects(tmp_path):
    a = await started(tmp_path, 'remote = true\npublic_url = "http://127.0.0.1:1/iot"', 'key = "right-key"')
    ota = f"http://127.0.0.1:{a.xz.port}/xiaozhi/ota/"
    try:
        b = FakeXiaozhi(ota, DEV)
        r = await b.ota()
        assert r["websocket"]["url"] == "ws://127.0.0.1:1/iot/xiaozhi/v1/" and "token" not in r["websocket"]
        local = f"ws://127.0.0.1:{a.xz.port}/xiaozhi/v1/"   # 测试里不过云端，直连本机
        for token in ("wrong-key", ""):
            b.ota_reply = {"websocket": {"url": local, "token": token}}
            with pytest.raises(aiohttp.WSServerHandshakeError):
                await b.connect()
        b.ota_reply = {"websocket": {"url": local, "token": "right-key"}}
        hello = await b.connect()
        assert hello["type"] == "hello"
        await b.close()
        # 没绑定的板子（没钥匙）远程模式下连 OTA 都不给
        other = FakeXiaozhi(ota, "aa:bb:cc:dd:ee:99")
        with pytest.raises(aiohttp.ClientResponseError):
            await other.ota()
        await other.close()
    finally:
        await a.xz.close()


async def read_sse(resp, n: int, timeout: float = 3.0) -> list[dict]:
    out = []
    while len(out) < n:
        line = await asyncio.wait_for(resp.content.readline(), timeout)
        if line.startswith(b"data: "):
            out.append(json.loads(line[6:]))
    return out


async def test_dial_page_and_sse_follow_state(tmp_path):
    a = await started(tmp_path)
    base = f"http://127.0.0.1:{a.xz.port}"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(base + "/dial") as r:
                assert r.status == 200 and "EventSource" in await r.text()
            async with http.get(base + "/dial/events") as r:
                first = await read_sse(r, 1)
                assert first[0]["role"] == "ju" and first[0]["state"] == "idle" and first[0]["board"] is False
                for s in ("thinking", "speaking", "idle"):
                    a.dial.set("ju", s)
                got = await read_sse(r, 3)
                assert [m["state"] for m in got] == ["thinking", "speaking", "idle"]
                # 板子上线：网页也知道
                b = FakeXiaozhi(base + "/xiaozhi/ota/", DEV)
                await b.connect()
                while True:
                    m = (await read_sse(r, 1))[0]
                    if m["board"]:
                        break
                await b.close()
    finally:
        await a.xz.close()


async def fake_github(tmp_path, data: bytes, sums: str | None):
    asset = "xiaozhi-spotpear-sp-esp32-s3-1.28-box.bin"

    async def latest(req):
        assets = [{"name": asset, "browser_download_url": f"{base}/dl/{asset}"}]
        if sums is not None:
            assets.append({"name": "SHA256SUMS", "browser_download_url": f"{base}/dl/SHA256SUMS"})
        return web.json_response({"tag_name": "v0.0.1-test", "assets": assets})

    async def dl(req):
        return web.Response(body=data if req.match_info["n"] == asset else sums.encode())

    app = web.Application()
    app.router.add_get("/repos/o/r/releases/latest", latest)
    app.router.add_get("/dl/{n}", dl)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    return runner, base, asset


async def test_release_download_checks_sha(tmp_path):
    data = b"firmware-bytes"
    good = f"{hashlib.sha256(data).hexdigest()}  xiaozhi-spotpear-sp-esp32-s3-1.28-box.bin\n"
    runner, base, asset = await fake_github(tmp_path, data, good)
    got = []
    try:
        r = ReleaseFirmware("o/r", asset, tmp_path / "fw", lambda p, t: got.append((p, t)), log=lambda s: None, api=base)
        p = await r.check()
        assert p is not None and p.read_bytes() == data and got == [(p, "v0.0.1-test")]
        assert await r.check() is None and len(got) == 1   # 同一个 tag 不重复
    finally:
        await runner.cleanup()
    runner, base, asset = await fake_github(tmp_path, data, "0" * 64 + f"  {asset}\n")
    try:
        r = ReleaseFirmware("o/r", asset, tmp_path / "fw2", lambda p, t: got.append((p, t)), log=lambda s: None, api=base)
        assert await r.check() is None and not (tmp_path / "fw2").exists()
    finally:
        await runner.cleanup()


async def test_app_uses_release_firmware(tmp_path):
    a = await started(tmp_path, 'firmware_release = "o/r"')
    try:
        assert a.release is not None and a.xz.firmware is None
        p = tmp_path / "x.bin"
        p.write_bytes(b"x")
        a._new_firmware(p, "v1")
        assert a.xz.firmware == p
    finally:
        await a.xz.close()


def test_firmware_and_release_exclusive(tmp_path):
    with pytest.raises(ValueError):
        load(cfg(tmp_path, 'firmware = "a.bin"\nfirmware_release = "o/r"'))
