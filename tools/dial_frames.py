"""用网页表盘的动画离线录圆屏帧，或核对静止帧。要先起表盘（tools/dial_demo.py 或设备服务），要 playwright。

    python tools/dial_frames.py --url http://127.0.0.1:8799/dial?role=ju --check            静止帧 vs 母图逐像素
    python tools/dial_frames.py --url ... --size 360 --out skins/ju/pastel/frames-360.bin     录三套循环

产物 frames-<size>.bin（设备服务原样发给板子，固件 rh_dial_anim 解析），小端：
    "RHA1" | u16 size | u8 fps | u8 nsets
    每套：u8 kind(0 待机 1 思考 2 说话底图 3 嘴) | u16 n | [u16 x u16 y u16 w u16 h]（只有嘴这套有，其余全 0）| n × (u32 len + JPEG)
    嘴这套 3 张：闭、半张、张开，贴在说话底图 (x,y) 处；说话时只刷这一小块。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.async_api import async_playwright

FPS = 12            # 来源：交接 SPEC「达不到 12fps 就降级」：按 12 帧/秒录
FRAMES = 24         # 来源：交接 SPEC「约 24 帧/套」，即 2 秒一圈
QUALITY = 80        # JPEG 质量。来源：实测 360² 帧包 1.7–2.0MB、240² 约 0.9MB，加两块帧缓冲仍在 1.85C 的 8MB PSRAM 内
HIDE = ".pill,.ring,.vignette,.glint,.initial,.sub,header{display:none!important}"   # 板子自己画名牌和状态，帧里只要角色和气泡


async def open_page(url: str, size: int):
    p = await async_playwright().start()
    b = await p.chromium.launch(args=["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"])
    pg = await b.new_page(viewport={"width": size + 40, "height": size + 40}, device_scale_factor=1)
    pg.on("pageerror", lambda e: print("页面报错：", e, file=sys.stderr))
    await pg.goto(url)
    await pg.add_style_tag(content=f"body{{--size:{size}px!important}} .bezel{{width:{size}px!important;height:{size}px!important}} {HIDE}")
    await pg.wait_for_function("Object.values(figures).some(f => f.fig)", timeout=20000)
    await pg.evaluate("Object.values(figures).forEach(f => f.fig && f.fig.stop())")
    return p, b, pg


async def check(url: str, mother: Path) -> int:
    p, b, pg = await open_page(url, 1024)
    # 按母图原尺寸画一帧（来源：SPEC 静止时逐像素对齐母图，所以用母图像素数）
    data = await pg.evaluate("""async () => {
        const f = Object.values(figures).find(f => f.fig);
        const box = document.createElement('div'); document.body.appendChild(box);
        const m = await import(BASE + 'static/dial-figure.js');
        const g = await m.mountFigure(box, f.m.skin_base + f.skin + '/', {pixels: f.fig.skin.size[0], manual: true});
        g.draw(g.rest()); const u = g.canvas.toDataURL('image/png'); g.dispose(); return u; }""")
    await b.close(); await p.stop()
    import base64
    got = np.asarray(Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[1]))).convert("RGB")).astype(int)
    ref = np.asarray(Image.open(mother).convert("RGB").resize((1024, 1024))).astype(int)
    d = np.abs(got - ref)
    print(f"静止帧 vs 母图：最大差 {d.max()}，平均差 {d.mean():.4f}，差>2 的像素 {(d.max(-1) > 2).mean() * 100:.3f}%")
    return 0 if d.max() <= 2 else 1


async def record(url: str, size: int, out: Path) -> None:
    p, b, pg = await open_page(url, size)
    scr = pg.locator(".card .screen").first

    async def shot(state: str, t: float, blink=False, mouth="") -> Image.Image:
        await pg.evaluate("([s, t, bl, mo, loop]) => { const f = Object.values(figures).find(f => f.fig).fig; f.draw(f.pose(s, t, {loop, blink: bl, mouth: mo})); }",
                          [state, t, blink, mouth, FRAMES / FPS])
        return Image.open(io.BytesIO(await scr.screenshot(type="png"))).convert("RGB")

    def jpg(im: Image.Image) -> bytes:
        # 4:4:4 不降采样：MCU 8 像素，240/360 都整除，固件解码输出正好 size×size（来源：esp_new_jpeg 按 MCU 对齐输出）
        bio = io.BytesIO(); im.save(bio, "JPEG", quality=QUALITY, optimize=True, subsampling=0); return bio.getvalue()

    sets = []
    # 待机一圈里眨一次眼（第 20、21 帧，来源：SPEC 眨眼 2–5 秒，一圈 2 秒眨一次在上限内）
    sets.append((0, (0, 0, 0, 0), [jpg(await shot("idle", i / FPS, blink=i in (20, 21))) for i in range(FRAMES)]))
    sets.append((1, (0, 0, 0, 0), [jpg(await shot("thinking", i / FPS)) for i in range(FRAMES)]))
    talk = await shot("speaking", 0)
    mouths = [talk, await shot("speaking", 0, mouth="mouth_half"), await shot("speaking", 0, mouth="mouth_open")]
    arr = [np.asarray(m).astype(int) for m in mouths]
    diff = np.zeros(arr[0].shape[:2], bool)
    for a in arr[1:]:
        diff |= np.abs(a - arr[0]).max(-1) > 6
    ys, xs = np.nonzero(diff)
    pad = 4
    x0, y0 = max(0, xs.min() - pad) & ~1, max(0, ys.min() - pad) & ~1
    x1, y1 = min(size, xs.max() + pad + 1), min(size, ys.max() + pad + 1)
    w, h = (x1 - x0 + 15) & ~15, (y1 - y0 + 15) & ~15     # JPEG 解码按 16 像素块，宽高取 16 的倍数
    rect = (int(x0), int(y0), int(min(w, size - x0)), int(min(h, size - y0)))
    sets.append((2, (0, 0, 0, 0), [jpg(talk)]))
    sets.append((3, rect, [jpg(m.crop((rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]))) for m in mouths]))
    await b.close(); await p.stop()
    buf = bytearray(b"RHA1" + struct.pack("<HBB", size, FPS, len(sets)))
    for kind, r, frames in sets:
        buf += struct.pack("<BH4H", kind, len(frames), *r)
        for f in frames:
            buf += struct.pack("<I", len(f)) + f
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(buf))
    print(f"写好 {out}：{len(buf) // 1024} KB，待机/思考各 {FRAMES} 帧，嘴块 {rect}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--mother", help="核对用的母图（默认 skins 里的 base.png）")
    ap.add_argument("--size", type=int, default=360)
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.check:
        return asyncio.run(check(a.url, Path(a.mother or "skins/ju/standard/base.png")))
    asyncio.run(record(a.url, a.size, Path(a.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
