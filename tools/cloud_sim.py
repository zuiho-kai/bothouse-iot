"""云端模拟自检：不用真板子、不用云服务器，本机把 docs/remote.md 那条「出门」链路整条搭起来跑一遍。

    python tools/cloud_sim.py                 # 有 nginx 用 nginx，没有用内置的转发
    python tools/cloud_sim.py --proxy nginx   # 一定用 nginx（CI 里这样跑，验的就是 deploy/remote 里那份配置）

    假板子 ──https/wss──▶ 模拟云端（nginx 照 deploy/remote/nginx-location.conf.example，或内置转发；自签证书、随机前缀）
           ──▶ 模拟反向隧道（本机 TCP 转发，扮演 ssh -R 127.0.0.1:18791:127.0.0.1:8791）──▶ 设备服务（remote = true）
           ──bridge-protocol──▶ 假 bothouse（收到一句就回一句）

验四件事：钥匙对的能连，没钥匙 / 钥匙错 / 没登记的板子被拒；语音往返（板子说一句 → bothouse 收到 → 回话变成 Opus 推回板子）；
表盘状态（板子上的 rh_dial 和经云端的网页表盘 SSE 都走 idle→thinking→speaking→idle）；隧道断了板子掉线，隧道恢复后板子照
真板的做法（先 OTA 再 WS）重连上，并能再说一句。

假板子照旧仓 10-21 真板日志（role-household logs/host-20261021-*.out）的样子：固件 2.5.2、协议 v1、先 POST OTA 再连回包里的
wss 地址、连上后先发 listen/start、断线后从 OTA 重来。说话用 ffmpeg 合成的音（不依赖 macOS say），转写用固定输出的假命令。
只在本机回环地址上起服务，全用临时目录，不碰线上 VPS、线上 bothouse 和任何聊天频道。要 Python 依赖、ffmpeg、openssl。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))

from bothouse_iot.app import BACKOFF_MIN  # noqa: E402
from bothouse_iot.app import App  # noqa: E402
from bothouse_iot.config import load  # noqa: E402
from bothouse_iot.mic import END_SILENCE_MS, MIN_UTTER_MS, PRE_ROLL_MS  # noqa: E402
from bothouse_iot.xiaozhi import encode_opus, find_ffmpeg  # noqa: E402
from fake_board import FakeXiaozhi  # noqa: E402

DEVICE = "aa:bb:cc:dd:ee:01"
STRANGER = "aa:bb:cc:dd:ee:99"
KEY = "sim-" + secrets.token_hex(8)
HEARD = "你好，今天天气怎么样"   # 沿用 tools/e2e.py
NGINX_EXAMPLE = ROOT / "deploy" / "remote" / "nginx-location.conf.example"
BAD_GATEWAY = web.HTTPBadGateway.status_code   # 上游连不上时回的，同 nginx
# 板子重试间隔：沿用 app.py BACKOFF_MIN（协议「断线重连」起始 1 秒）
RETRY_SEC = BACKOFF_MIN
# 假说话：时长取 mic.py MIN_UTTER_MS 的三倍（切句器不当噪声丢），前面留 PRE_ROLL_MS，后面静音取 END_SILENCE_MS 的两倍（切句器认出说完）
TONE_SEC = MIN_UTTER_MS * 3 / 1000
TONE_DELAY_MS = PRE_ROLL_MS
TONE_PAD_SEC = END_SILENCE_MS * 2 / 1000


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- 假 bothouse：bridge-protocol 的服务端，收到一句回一句 ----------
class FakeBothouse:
    def __init__(self):
        self.port = free_port()
        self.got: list[dict] = []
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/bridge/ws", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def _ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        async for m in ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            d = json.loads(m.data)
            t = d.get("type")
            if t == "register":
                await ws.send_json({"type": "register_ack", "ok": True})
            elif t == "ping":
                await ws.send_json({"type": "pong", "ts": d.get("ts")})
            elif t == "message":
                self.got.append(d)
                to = {"session_key": d["session_key"], "reply_ctx": d.get("reply_ctx")}
                await ws.send_json({"type": "typing_start", **to})
                await asyncio.sleep(0.3)
                await ws.send_json({"type": "reply", **to, "content": f"收到：{d.get('content')}"})
                await ws.send_json({"type": "typing_stop", **to})
        return ws

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


# ---------- 模拟反向隧道：扮演 ssh -R，断开时连着的连接一起断 ----------
class Tunnel:
    def __init__(self, target: int):
        self.port, self.target = free_port(), target
        self._server: asyncio.base_events.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._conn, "127.0.0.1", self.port)

    async def _conn(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_connection("127.0.0.1", self.target)
        except OSError:
            w.close()
            return
        pair = {w, uw}
        self._writers |= pair

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                dst.close()
        await asyncio.gather(pump(r, uw), pump(ur, w))
        self._writers -= pair

    async def stop(self) -> None:
        """隧道断了：不再接新连接，已有的连接也断（ssh 进程没了就是这样）。"""
        if self._server is not None:
            self._server.close()
            for w in list(self._writers):
                w.close()
            self._writers.clear()
            await self._server.wait_closed()
            self._server = None


# ---------- 模拟云端：nginx（照 deploy/remote 的配置）或内置转发 ----------
def self_signed(d: Path) -> tuple[Path, Path]:
    crt, key = d / "cloud.crt", d / "cloud.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
                    "-keyout", str(key), "-out", str(crt)], check=True, capture_output=True)
    return crt, key


class NginxCloud:
    """把 deploy/remote/nginx-location.conf.example 原样放进一个只听 127.0.0.1 的 ssl server，换上前缀和隧道端口。"""

    def __init__(self, tmp: Path, prefix: str, tunnel_port: int, crt: Path, key: Path):
        self.dir, self.port = tmp / "nginx", free_port()
        self.prefix, self.tunnel_port, self.crt, self.key = prefix, tunnel_port, crt, key
        self.proc: subprocess.Popen | None = None

    def conf(self) -> str:
        loc = "\n".join(ln for ln in NGINX_EXAMPLE.read_text().splitlines() if not ln.lstrip().startswith("#"))
        loc = loc.replace("<随机前缀>", self.prefix).replace("127.0.0.1:18791", f"127.0.0.1:{self.tunnel_port}")
        t = self.dir
        return f"""daemon off; worker_processes 1; pid {t}/nginx.pid; error_log {t}/error.log warn;
events {{ worker_connections 64; }}
http {{
  access_log {t}/access.log;
  client_body_temp_path {t}/body; proxy_temp_path {t}/proxy; fastcgi_temp_path {t}/fcgi;
  uwsgi_temp_path {t}/uwsgi; scgi_temp_path {t}/scgi;
  server {{
    listen 127.0.0.1:{self.port} ssl;
    server_name localhost;
    ssl_certificate {self.crt}; ssl_certificate_key {self.key};
{loc}
  }}
}}
"""

    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "nginx.conf").write_text(self.conf())
        self.proc = subprocess.Popen(["nginx", "-p", str(self.dir), "-c", str(self.dir / "nginx.conf")],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.1):
                    return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("nginx 没起来：" + self.proc.stderr.read().decode())
                await asyncio.sleep(0.1)
        raise RuntimeError("nginx 5 秒没开端口")

    async def close(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
            self.proc.wait(5)


class PyCloud:
    """没有 nginx 时的内置转发：做那份 nginx location 做的事（去前缀、透传 WS 和头、SSE 不攒）。"""
    PASS = ("authorization", "device-id", "client-id", "protocol-version", "content-type", "accept")

    def __init__(self, prefix: str, tunnel_port: int, crt: Path, key: Path):
        self.port, self.prefix, self.up, self.crt, self.key = free_port(), prefix, tunnel_port, crt, key
        self._runner: web.AppRunner | None = None
        self.http: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(self.crt, self.key)
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        app = web.Application()
        app.router.add_route("*", f"/{self.prefix}/{{tail:.*}}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port, ssl_context=ctx).start()

    async def _handle(self, req: web.Request) -> web.StreamResponse:
        url = f"http://127.0.0.1:{self.up}/{req.match_info['tail']}" + (f"?{req.query_string}" if req.query_string else "")
        hdrs = {k: v for k, v in req.headers.items() if k.lower() in self.PASS}
        if req.headers.get("Upgrade", "").lower() == "websocket":
            return await self._ws(req, url, hdrs)
        try:
            async with self.http.request(req.method, url, headers=hdrs, data=await req.read()) as r:
                out = web.StreamResponse(status=r.status, headers={k: v for k, v in r.headers.items()
                                                                     if k.lower() in ("content-type", "cache-control")})
                await out.prepare(req)
                try:
                    async for chunk in r.content.iter_any():
                        await out.write(chunk)
                except (aiohttp.ClientError, ConnectionError):
                    pass
                return out
        except (aiohttp.ClientError, OSError):
            return web.Response(status=BAD_GATEWAY, text="bad gateway")

    async def _ws(self, req: web.Request, url: str, hdrs: dict) -> web.StreamResponse:
        try:
            up = await self.http.ws_connect(url, headers=hdrs, autoping=False, max_msg_size=0)
        except aiohttp.WSServerHandshakeError as e:
            return web.Response(status=e.status)
        except (aiohttp.ClientError, OSError):
            return web.Response(status=BAD_GATEWAY, text="bad gateway")
        down = web.WebSocketResponse(autoping=False, max_msg_size=0)
        await down.prepare(req)

        async def pump(src, dst) -> None:
            async for m in src:
                if m.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(m.data)
                elif m.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(m.data)
                elif m.type == aiohttp.WSMsgType.PING:
                    await dst.ping(m.data)
                elif m.type == aiohttp.WSMsgType.PONG:
                    await dst.pong(m.data)
                else:
                    break
            await dst.close()
        await asyncio.gather(pump(up, down), pump(down, up), return_exceptions=True)
        return down

    async def close(self) -> None:
        if self.http is not None:
            await self.http.close()
        if self._runner is not None:
            await self._runner.cleanup()


# ---------- 假板子（照真板的连法） ----------
async def board_connect(ota: str, device: str, key: str | None, deadline: float) -> FakeXiaozhi:
    """照真板的做法：先 OTA，再连回包里的 wss（带 NVS 里的钥匙），连上先发 listen/start；连不上隔一会儿从 OTA 重来。"""
    t0 = time.monotonic()
    while True:
        b = FakeXiaozhi(ota, device, version=1, fw="2.5.2", ssl=False)
        try:
            await b.ota()
            b.ota_raw = json.loads(json.dumps(b.ota_reply))   # 原样留一份回包：下面塞的钥匙是板子 NVS 里的，不是回包里的
            if key is not None:
                b.ota_reply["websocket"]["token"] = key
            await b.connect()
            await b.ws.send_json({"session_id": "", "type": "listen", "state": "start", "mode": "auto"})
            return b
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            await b.close()
            if time.monotonic() - t0 > deadline:
                raise
            await asyncio.sleep(RETRY_SEC)


def tone() -> bytes:
    """ffmpeg 合成一段音（WAV 16k 单声道）代替说话，前后带静音，切句器能切出一句。"""
    return subprocess.run([find_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", f"sine=duration={TONE_SEC}",
                           "-af", f"adelay={TONE_DELAY_MS},apad=pad_dur={TONE_PAD_SEC}",
                           "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"], capture_output=True, check=True).stdout


class Checks:
    def __init__(self):
        self.failed: list[str] = []

    def __call__(self, ok: bool, what: str, detail: str = "") -> bool:
        print(f"{'OK  ' if ok else 'FAIL'} {what}" + (f"：{detail}" if detail else ""), flush=True)
        if not ok:
            self.failed.append(what)
        return ok


async def say_once(board: FakeXiaozhi, bh: FakeBothouse, packets: list[bytes], timeout: float):
    """板子说一句，等回话播完。返回（bothouse 收到的、板子收到的音频帧数、字幕、板子表盘状态序列）。"""
    n0, g0 = len(bh.got), len(board.got)
    await board.speak_into_mic(packets)
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if any(k == "json" and d.get("type") == "tts" and d.get("state") == "stop" for _, k, d in board.got[g0:]):
            break
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.3)
    new = board.got[g0:]
    subs = [d.get("text") for _, k, d in new if k == "json" and d.get("type") == "tts" and d.get("state") == "sentence_start"]
    frames = sum(1 for _, k, _d in new if k == "audio")
    dials = [d.get("state") for _, k, d in new if k == "json" and d.get("type") == "rh_dial"]
    return [m.get("content") for m in bh.got[n0:]], frames, subs, dials


async def main(a) -> int:
    if a.proxy == "nginx" and not shutil.which("nginx"):
        print("FAIL 要 --proxy nginx 但找不到 nginx")
        return 2
    use_nginx = a.proxy == "nginx" or (a.proxy == "auto" and shutil.which("nginx"))
    tmp = Path(tempfile.mkdtemp(prefix="bhiot-cloudsim-"))
    prefix = secrets.token_hex(12)
    check = Checks()
    logs: list[str] = []

    def log(s: str) -> None:
        logs.append(s)
        if a.verbose:
            print("    [设备服务] " + s, flush=True)

    bh = FakeBothouse()
    await bh.start()
    dev_port = free_port()
    tunnel = Tunnel(dev_port)
    crt, key = self_signed(tmp)
    cloud = NginxCloud(tmp, prefix, tunnel.port, crt, key) if use_nginx else PyCloud(prefix, tunnel.port, crt, key)
    public = f"https://127.0.0.1:{cloud.port}/{prefix}"
    tts = [find_ffmpeg(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=duration={TONE_SEC}", "{out}"]
    cfg = tmp / "config.toml"
    cfg.write_text(f'''
[device]
listen = "127.0.0.1"
port = {dev_port}
remote = true
public_url = "{public}"
data_dir = "{tmp}/data"
[bothouse]
url = "ws://127.0.0.1:{bh.port}/bridge/ws"
platform = "iot"
[speech]
stt = "command"
stt_command = ["/bin/echo", "{HEARD}"]
tts = "command"
tts_command = {json.dumps(tts)}
[[boards]]
device = "{DEVICE}"
role = "ju"
name = "橘"
project = "iot-sim"
key = "{KEY}"
''')
    app = App(load(cfg), log=log)
    runner = asyncio.ensure_future(app.run())
    boards: list[FakeXiaozhi] = []
    http = aiohttp.ClientSession()
    watcher = sse = None
    try:
        await asyncio.wait_for(app.registered.wait(), a.timeout)
        await tunnel.start()
        await cloud.start()
        ota = f"{public}/xiaozhi/ota/"
        print(f"链路：假板子 → 模拟云端（{'nginx，deploy/remote 那份配置' if use_nginx else '内置转发'}）https://127.0.0.1:{cloud.port}/<前缀>"
              f" → 模拟隧道 :{tunnel.port} → 设备服务 :{dev_port} → 假 bothouse :{bh.port}", flush=True)

        # 1 钥匙
        async with http.post(ota, json={"application": {"version": "2.5.2"}}, headers={"Device-Id": STRANGER}, ssl=False) as r:
            check(r.status == web.HTTPForbidden.status_code, "没登记的板子 OTA 被拒", f"HTTP {r.status}")
        async with http.post(f"https://127.0.0.1:{cloud.port}/xiaozhi/ota/", json={}, headers={"Device-Id": DEVICE},
                             ssl=False) as r:
            check(r.status == web.HTTPNotFound.status_code, "不带随机前缀的地址进不去", f"HTTP {r.status}")
        for wrong in (None, "wrong-key"):
            what = f"{'钥匙错' if wrong else '没钥匙'}的板子被拒"
            bad = FakeXiaozhi(ota, DEVICE, ssl=False)
            try:
                await bad.ota()
                if wrong:
                    bad.ota_reply["websocket"]["token"] = wrong
                await bad.connect()
                check(False, what, "居然连上了")
            except aiohttp.WSServerHandshakeError as e:
                check(e.status == web.HTTPUnauthorized.status_code, what, f"WS {e.status}")
            finally:
                await bad.close()

        sse = await http.get(f"{public}/dial/events", ssl=False)
        web_states: list[str] = []

        async def watch(resp) -> None:
            try:
                async for line in resp.content:
                    if line.startswith(b"data: "):
                        m = json.loads(line[6:])
                        if m.get("role") == "ju" and (not web_states or web_states[-1] != m["state"]):
                            web_states.append(m["state"])
            except (aiohttp.ClientError, asyncio.CancelledError):
                pass
        watcher = asyncio.ensure_future(watch(sse))

        board = await board_connect(ota, DEVICE, KEY, a.timeout)
        boards.append(board)
        url = board.ota_reply["websocket"]["url"]
        check(url == f"wss://127.0.0.1:{cloud.port}/{prefix}/xiaozhi/v1/", "OTA 回的是云端 wss 地址", url.replace(prefix, "<前缀>"))
        check("token" not in board.ota_raw["websocket"], "OTA 回包不带 token（不覆盖板子 NVS 里的钥匙）")
        await asyncio.sleep(0.3)
        check(any("ju 的板子" in s and "协议 v1" in s and "经 " + public in s for s in logs), "钥匙对的板子经云端连上（协议 v1）")

        # 2 语音往返 + 3 表盘
        packets = encode_opus(tone())
        sent, frames, subs, dials = await say_once(board, bh, packets, a.timeout)
        check(sent == [HEARD], "板子说的一句到了 bothouse", repr(sent))
        check(any(HEARD[:4] in (s or "") for s in subs) and frames > 0, "bot 回话变成语音推回板子", f"字幕 {subs}，音频 {frames} 帧")
        check(dials[:3] == ["thinking", "speaking", "idle"], "板子表盘 thinking→speaking→idle", str(dials))
        await asyncio.sleep(0.5)
        check(web_states[:4] == ["idle", "thinking", "speaking", "idle"], "网页表盘（经云端 SSE）idle→thinking→speaking→idle",
              str(web_states))

        # 4 断线重连
        await tunnel.stop()
        t0 = time.monotonic()
        while not board.ws.closed and time.monotonic() - t0 < a.timeout:
            await asyncio.sleep(0.1)
        check(board.ws.closed, "隧道断了板子掉线", f"{time.monotonic() - t0:.1f} 秒")
        await board.close()
        reconnect = asyncio.ensure_future(board_connect(ota, DEVICE, KEY, a.timeout))
        await asyncio.sleep(RETRY_SEC * 2.5)   # 隧道断着的这段，板子按间隔重试 OTA，拿到 502
        await tunnel.start()
        t0 = time.monotonic()
        board = await reconnect
        boards.append(board)
        check(True, "隧道恢复后板子重连上（先 OTA 再 WS）", f"{time.monotonic() - t0:.1f} 秒")
        await asyncio.sleep(0.3)
        sent, frames, subs, _ = await say_once(board, bh, packets, a.timeout)
        check(sent == [HEARD] and frames > 0, "重连后再说一句也通", f"bothouse 收到 {sent}，音频 {frames} 帧")
    finally:
        if watcher is not None:
            watcher.cancel()
        if sse is not None:
            sse.close()
        for b in boards:
            await b.close()
        await http.close()
        await app.close()
        runner.cancel()
        await cloud.close()
        await tunnel.stop()
        await bh.close()
        shutil.rmtree(tmp, ignore_errors=True)
    if check.failed and not a.verbose:
        print("设备服务日志（最后几行）：\n    " + "\n    ".join(logs[-30:]))
    print("CLOUD SIM PASS" if not check.failed else f"CLOUD SIM FAIL：{'、'.join(check.failed)}")
    return 0 if not check.failed else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--proxy", choices=["auto", "nginx", "python"], default="auto",
                    help="模拟云端用什么：nginx（照 deploy/remote 配置）/ python（内置转发）/ auto（有 nginx 用 nginx）")
    ap.add_argument("--timeout", type=float, default=60, help="每步最多等几秒（沿用 tools/e2e.py 的默认）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印设备服务日志")
    sys.exit(asyncio.run(main(ap.parse_args())))
