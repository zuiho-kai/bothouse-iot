"""闭眼贴片（描线画风用，build_skin.py 之后再跑）：眼睛那块换成母图肤色，只取闭眼图里的描线和头发贴上，覆盖 eyes_closed。
整张重画的闭眼图会挪动挡眼的刘海线，按 build_skin 的整块贴法会留重影（涂鸦皮肤实测），所以换这种取法。

    python tools/skin/lash_closed.py <皮肤目录> <母图> <闭眼图（和母图同尺寸）> '<眼睛范围 [[x0,y0,x1,y1],...] 椭圆外接框>'
涂鸦皮肤：python tools/skin/lash_closed.py skins/ju/doodle mother1024.png closed1024.png '[[398,398,476,466],[386,386,468,428],[530,372,606,438]]'
"""
import json, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
from build_skin import fill_skin, ellipse_mask
d, mp, cp, boxes = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
mom = np.asarray(Image.open(mp).convert('RGB')).astype(np.float32)
cl = np.asarray(Image.open(cp).convert('RGB')).astype(np.float32)
size = (mom.shape[1], mom.shape[0])
hole = np.array(ellipse_mask(size, boxes)) > 0
# 皮肤：取眼睛周围亮像素的中位色，加一点母图纸纹
ring = (np.array(ellipse_mask(size, boxes, grow=14)) > 0) & ~hole
skin = np.median(mom[ring & (mom.mean(-1) > 190) & ((mom[..., 0] - mom[..., 2]) < 45)], 0); print("skin", skin)
rng = np.random.default_rng(1)
noise = np.asarray(Image.fromarray((rng.normal(128, 18, mom.shape[:2])).clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7))).astype(np.float32) - 128
out = mom.copy()
out[hole] = np.clip(skin + noise[hole][:, None] * 0.3, 0, 255)
# 闭眼图里的线和头发（深色、橙色）照搬，皮肤一律换成母图肤色
lum = cl.mean(-1)
ink = np.clip((150 - lum) / 60, 0, 1)
orange = np.clip(((cl[..., 0] - cl[..., 2]) - 90) / 40, 0, 1) * (lum < 200)
k = np.maximum(ink, orange) * hole
out = out * (1 - k[..., None]) + cl * k[..., None]
a = np.asarray(ellipse_mask(size, boxes, grow=3, blur=2.5)).astype(np.float32)
b = np.array(boxes); x0, y0 = int(b[:, 0].min()) - 10, int(b[:, 1].min()) - 10; x1, y1 = int(b[:, 2].max()) + 10, int(b[:, 3].max()) + 10
rgba = np.dstack([out[y0:y1, x0:x1], a[y0:y1, x0:x1]]).astype(np.uint8)
Image.fromarray(rgba, 'RGBA').save(f'{d}/eyes_closed.png', optimize=True)
s = json.load(open(f'{d}/skin.json'))
for p in s['parts']:
    if p['id'] == 'eyes_closed': p['box'] = [x0, y0, x1 - x0, y1 - y0]
open(f'{d}/skin.json', 'w').write(json.dumps(s, ensure_ascii=False, indent=1))
print('eyes_closed', x0, y0, x1 - x0, y1 - y0)
