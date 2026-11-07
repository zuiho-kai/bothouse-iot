"""把一张母图 + 本机出图模型补画的差分，做成表盘动画皮肤（skins/<角色>/<皮肤>/）。流程见 docs/dial-skins.md。

    python tools/skin/build_skin.py <角色素材描述.json> <输出皮肤目录>

描述文件（坐标都是母图像素）：
    {"mother": "mother.png",                     母图，方形（1024×1024 最好）
     "closed": "gen_closed.png", "closed_skin": "gen_noeyes.png",   闭眼：带睫毛线的那张 + 只有皮肤的那张（模型重画的整块 crop）
     "closed_band": [[x0,y0,x1,y1], ...],         从 closed 里取睫毛线的矩形，其余取 closed_skin
     "mouth_open": "gen_mouth.png",
     "crop": [x0, y0, size],                      上面几张 gen 是母图这块放大到 1024 后重画的
     "eyes": [{"open": [cx,cy,rx,ry], "iris": [cx,cy,rx,ry], "lift": 7}, ...],   眼白+虹膜露出的椭圆、虹膜椭圆、往上看挪几像素
     "eye_mask": [[x0,y0,x1,y1], ...],            闭眼贴片的范围（椭圆外接框）
     "mouth": [x0,y0,x1,y1],                      嘴的范围（椭圆外接框）
     "rig": {...}}                                原样写进 skin.json，见 docs/dial-skins.md
母图原样作为 base.png，所以静止时和母图逐像素一致；差分只在各自小块里，羽化边，颜色对齐到母图。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

FEATHER = 4      # 贴片羽化半径（像素）。来源：ju 素材实测，3 以下看得出边，6 以上盖不住原来的嘴
PAD = 6          # 贴片外扩（像素），给羽化留地方。来源：同上


def ellipse_mask(size, boxes, grow=0, blur=0):
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    for b in boxes:
        d.ellipse([b[0] - grow, b[1] - grow, b[2] + grow, b[3] + grow], fill=255)
    return m.filter(ImageFilter.GaussianBlur(blur)) if blur else m


def from_crop(path, crop, size):
    """gen 图是母图 crop 放大后重画的：缩回原尺寸，贴回母图坐标。"""
    x0, y0, s = crop
    g = Image.open(path).convert("RGB").resize((s, s), Image.LANCZOS)
    full = Image.new("RGB", size)
    full.paste(g, (x0, y0))
    return full


def match_color(src: np.ndarray, ref: np.ndarray, ring: np.ndarray) -> np.ndarray:
    """模型重画后颜色会偏一点：按贴片外圈和母图的均值差整体校正。"""
    if ring.sum() == 0:
        return src
    off = (ref[ring].mean(0) - src[ring].mean(0))
    return np.clip(src + off, 0, 255)


def patch(out_dir: Path, name: str, img: np.ndarray, alpha: np.ndarray, bbox):
    x0, y0, x1, y1 = [int(v) for v in bbox]
    rgba = np.dstack([img[y0:y1, x0:x1], alpha[y0:y1, x0:x1]]).astype(np.uint8)
    Image.fromarray(rgba, "RGBA").save(out_dir / f"{name}.png", optimize=True)
    return {"id": name, "tex": name, "box": [x0, y0, x1 - x0, y1 - y0]}


def fill_skin(img: np.ndarray, hole: np.ndarray, iters=400) -> np.ndarray:
    """把 hole 里的像素用周围皮肤抹平（拉普拉斯扩散）。嘴附近是平涂皮肤，够用。iters 来源：60 像素宽的洞实测 400 轮收敛。"""
    out = img.copy()
    ring = np.roll(hole, 3, 0) | np.roll(hole, -3, 0) | np.roll(hole, 3, 1) | np.roll(hole, -3, 1)
    out[hole] = img[ring & ~hole].mean(0)   # 初值取洞外一圈的皮肤色，不取整图（整图含黑背景，扩散不完会留暗圈）
    for _ in range(iters):
        avg = (np.roll(out, 1, 0) + np.roll(out, -1, 0) + np.roll(out, 1, 1) + np.roll(out, -1, 1)) / 4
        out[hole] = avg[hole]
    return out


def main(desc_path: str, out: str):
    desc_path = Path(desc_path)
    D = json.loads(desc_path.read_text())
    rel = lambda p: desc_path.parent / p
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    mom_img = Image.open(rel(D["mother"])).convert("RGB")
    size = mom_img.size
    mom = np.asarray(mom_img).astype(np.float32)
    mom_img.save(out_dir / "base.png", optimize=True)
    parts = [{"id": "base", "tex": "base", "box": [0, 0, size[0], size[1]], "grid": [32, 32], "z": 0}]
    textures = {"base": "base.png"}

    def add(name, img, alpha, bbox, z):
        p = patch(out_dir, name, img, alpha, bbox)
        p.update(grid=[4, 4], z=z, alpha=0)
        parts.append(p)
        textures[name] = f"{name}.png"

    def bbox_of(boxes, grow):
        b = np.array(boxes, dtype=float)
        return (max(0, b[:, 0].min() - grow), max(0, b[:, 1].min() - grow), min(size[0], b[:, 2].max() + grow), min(size[1], b[:, 3].max() + grow))

    # 闭眼：closed 的睫毛线 + closed_skin 的皮肤
    if D.get("closed"):
        c1 = np.asarray(from_crop(rel(D["closed"]), D["crop"], size)).astype(np.float32)
        c2 = np.asarray(from_crop(rel(D.get("closed_skin") or D["closed"]), D["crop"], size)).astype(np.float32)
        band = np.zeros(size[::-1], np.float32)
        for x0, y0, x1, y1 in D.get("closed_band", []):
            band[y0:y1, x0:x1] = 1
        band = np.asarray(Image.fromarray((band * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(3))).astype(np.float32)[..., None] / 255
        eye = c1 * band + c2 * (1 - band)
        a = np.asarray(ellipse_mask(size, D["eye_mask"], grow=2, blur=FEATHER)).astype(np.float32)
        ring = (a > 10) & (a < 120)
        eye = match_color(eye, mom, ring)
        add("eyes_closed", eye, a, bbox_of(D["eye_mask"], PAD + FEATHER * 2), 2)

    # 往上看：母图虹膜往上挪 lift 像素，露出的下沿补眼白
    th = mom.copy()
    opens = []
    for e in D["eyes"]:
        cx, cy, rx, ry = e["open"]
        lift = int(e.get("lift", 7))
        om = np.array(ellipse_mask(size, [(cx - rx, cy - ry, cx + rx, cy + ry)])) > 0
        ys, xs = np.nonzero(om)
        src_y = np.minimum(ys + lift, size[1] - 1)
        ok = om[src_y, xs]
        th[ys[ok], xs[ok]] = mom[src_y[ok], xs[ok]]
        # 露出来的地方：眼白色（取这只眼眼白最亮的一截的中位数），上沿带一点虹膜投影
        white = mom[om & (mom.sum(-1) > 650)]
        wc = np.median(white, 0) if len(white) else np.array([245, 238, 240], np.float32)
        th[ys[~ok], xs[~ok]] = wc
        opens.append((cx - rx, cy - ry, cx + rx, cy + ry))
    a = np.asarray(ellipse_mask(size, opens, grow=1, blur=1.5)).astype(np.float32)
    add("eyes_think", th, a, bbox_of(opens, PAD), 1)

    # 嘴：张开 = 模型画的；半张 = 张开的竖向压扁贴在抹平的皮肤上
    if D.get("mouth_open"):
        mb = D["mouth"]
        mo = np.asarray(from_crop(rel(D["mouth_open"]), D["crop"], size)).astype(np.float32)
        a = np.asarray(ellipse_mask(size, [mb], grow=2, blur=FEATHER)).astype(np.float32)
        ring = (a > 10) & (a < 120)
        mo = match_color(mo, mom, ring)
        bb = bbox_of([mb], PAD + FEATHER * 2)
        add("mouth_open", mo, a, bb, 3)
        # 半张：先把母图嘴抹成皮肤，再把张开的嘴（只取深色口腔部分）竖向压到 55% 贴上
        hole = np.array(ellipse_mask(size, [mb], grow=-2)) > 0
        skin = fill_skin(mom, hole)
        # 抹平的皮肤没有纸纹/笔触：借旁边脸颊的高频细节（往左挪一个嘴宽）。来源：油画棒皮肤实测，不补看得出一块光面
        dx = int(mb[2] - mb[0])
        donor = np.roll(mom, dx, axis=1)
        blur = np.asarray(Image.fromarray(donor.astype(np.uint8)).filter(ImageFilter.GaussianBlur(3))).astype(np.float32)
        skin[hole] = np.clip(skin[hole] + (donor - blur)[hole], 0, 255)
        x0, y0, x1, y1 = [int(v) for v in bb]
        cyc = (mb[1] + mb[3]) / 2
        sub = Image.fromarray(mo[y0:y1, x0:x1].astype(np.uint8))
        h = y1 - y0
        squ = sub.resize((x1 - x0, max(2, int(h * 0.55))), Image.LANCZOS)   # 来源：Coopanion figure.js:401 半张 sy≈.35+.65·open，open≈.3
        half = skin.copy()
        oy = int(cyc - squ.height / 2)
        sq = np.asarray(squ).astype(np.float32)
        lum = sq.mean(-1)
        dark = np.clip((np.median(lum) - lum - 25) / 40, 0, 1)[..., None]   # 比周围皮肤暗 25 以上的才是口腔（来源：ju 素材实测，口腔比皮肤暗 80+，唇下阴影 10–20）
        region = half[oy:oy + squ.height, x0:x1]
        half[oy:oy + squ.height, x0:x1] = sq * dark + region * (1 - dark)
        add("mouth_half", half, a, bb, 3)

    skin = {"size": list(size), "textures": textures, "parts": parts, "rig": D["rig"]}
    for k in ("name", "view"):
        if k in D:
            skin[k] = D[k]
    (out_dir / "skin.json").write_text(json.dumps(skin, ensure_ascii=False, indent=1))
    print("写好", out_dir, [p["id"] for p in parts])


if __name__ == "__main__":
    main(*sys.argv[1:3])
