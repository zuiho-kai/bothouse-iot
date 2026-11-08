// 表盘角色动画：一张立绘铺在细网格上，用 Coopanion 的网格变形渲染器（vendor/coopanion/rig.js）画。
// 头、发梢、呼吸都是平滑的位移场，所以动起来不会露洞、不漂脸；眼睛和嘴是贴在原位的小贴片（闭眼、往上看、半张、张开）。
// 素材格式见 docs/dial-skins.md：skins/<角色>/<皮肤>/skin.json + 贴图，坐标一律是母图像素。
//
//   const fig = await mountFigure(screenEl, "dial/skin/ju/standard/");   // 失败抛错，调用方退回静态头像
//   fig.set("thinking");  fig.set("speaking", {env: [0.1, 0.6, ...], envMs: 50});  fig.dispose();
//
// 录圆屏帧（tools/dial_frames.py）用 fig.pose(state, t, {loop, blink, mouth}) + fig.draw()：不走弹簧，周期取整到 loop 秒，首尾能接上。
//
// 动作幅度的来源（SPEC = 交接 handoff-bhiot-dialanim-1108「范围·网页端」）：呼吸缩放 ±0.6%、头和发梢 ±2° 正弦+弹簧、
// 随机眨眼 2–5 秒、思考歪头+三个气泡、状态切换 0.2–0.3 秒缓动。弹簧写法和参数量级沿用 Coopanion whale/figure.js:48、483-486。
import { createRig } from "./vendor/coopanion/rig.js";

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const lerp = (a, b, k) => a + (b - a) * k;
const ease = (rate, dt) => 1 - Math.exp(-rate * dt);
const smooth = (a, b, x) => { const k = clamp((x - a) / (b - a), 0, 1); return k * k * (3 - 2 * k); };
const TAU = Math.PI * 2;

function spring(k, c) {   // 沿用 Coopanion whale/figure.js:48 的弹簧
  return { x: 0, v: 0, step(target, dt) { this.v += ((target - this.x) * k - this.v * c) * dt; this.x += this.v * dt; return this.x; } };
}

function loadImage(src) {
  return new Promise((ok, bad) => { const im = new Image(); im.onload = () => ok(im); im.onerror = () => bad(new Error("贴图加载失败：" + src)); im.src = src; });
}

// 各状态的姿势目标：tilt 头的转角（度，负=往左歪），ty 头上下（母图像素，负=上），eyes 用哪张眼睛贴片。
// 来源：SPEC「思考歪头、眼神上移」；歪头角度参照 Coopanion whale/figure.js:538 HEAD_TILT.thinking=-8，圆屏小，取一半多
const POSE = {
  idle: { tilt: 0, ty: 0, eyes: "" },
  thinking: { tilt: -4.5, ty: -6, eyes: "eyes_think" },   // 来源：见上
  speaking: { tilt: 0.6, ty: 0, eyes: "" },               // 来源：SPEC 说话时头跟着轻点（幅度在 ±2° 内）
};
const SWITCH_RATE = 12;   // 来源：SPEC 状态切换 0.2–0.3 秒：1-exp(-12×0.25)=95%
const ENV_MS = 50;        // 沿用设备服务 dial.py 的 ENV_MS（音量包络每格毫秒）

export async function mountFigure(screen, base, opts = {}) {
  const skin = await (await fetch(base + "skin.json", { cache: "no-cache" })).json();
  const [W, H] = skin.size;
  const R = skin.rig;
  const texNames = Object.keys(skin.textures);
  const images = Object.fromEntries(await Promise.all(texNames.map(async k => [k, await loadImage(base + skin.textures[k])])));   // 先全部加载完再开画，不闪

  const canvas = document.createElement("canvas");
  canvas.className = "figure";
  const dpr = Math.min(2, window.devicePixelRatio || 1);   // 来源：母图 1024 像素，420px 表盘在 2 倍屏≈840 已接近母图，再高没有细节可加
  const px = opts.pixels || Math.round(Math.min(W, (screen.clientWidth || W) * dpr));
  canvas.width = canvas.height = px;

  // 变形器：发梢摆 → 头转 → 整体呼吸（rig.js 从里到外套）
  const [ncx, ncy] = R.neck;
  const [hcx, hcy, hrx, hry] = R.head;
  const hf = R.headFeather ?? 0.3;   // 来源：skin.json 不写时的默认，ju 素材实测头发过渡不起褶
  const headW = (x, y) => { const r = Math.hypot((x - hcx) / hrx, (y - hcy) / hry); return 1 - smooth(1, 1 + hf, r); };
  const hairW = (x, y, h) => {
    const [x0, y0, x1, y1, side] = h;
    // 外侧 65% 宽度满幅、靠里渐弱到 0（来源：ju 素材实测，里侧是身体，不能跟着摆）
    const ux = side === "L" ? smooth(x1, x0 + (x1 - x0) * 0.35, x) : smooth(x0, x1 - (x1 - x0) * 0.35, x);
    return ux * Math.pow(smooth(y0, y1, y), 1.4);   // 越往发梢摆得越多（来源：Coopanion hair 变形 v*v 的做法，略收）
  };
  const model = {
    view: skin.view || [0, 0, W, H],
    deformers: {
      all: { kind: "rot", pivot: R.ground || [W / 2, H] },
      head: { kind: "warp", rect: [0, 0, W, H], parent: "all" },
      hair: { kind: "warp", rect: [0, 0, W, H], parent: "head" },
    },
    parts: skin.parts.map(p => ({ ...p, parent: p.parent || "hair" })),
  };
  let rig;
  try { rig = createRig(canvas, model); } catch (e) { throw new Error("WebGL 不可用：" + e.message); }
  for (const k of texNames) rig.upload(k, images[k]);

  // ---------- 每帧状态 ----------
  // 弹簧参数沿用 Coopanion whale/figure.js:483-485（head 70/10、hair 55/7），发梢放软一点
  const sp = { tilt: spring(70, 11), ty: spring(70, 11), hairL: spring(40, 5), hairR: spring(40, 5) };
  let state = "idle", env = null, envT0 = 0, envMs = ENV_MS;
  const eyeK = {};          // 眼睛贴片当前不透明度（缓动）
  let blinkAt = 0, blinkEnd = -1, mouthLvl = 0, mouthSprite = "", bubbleK = 0;
  let last = performance.now() / 1000, raf = 0, idleSkip = 0;
  const t0 = last;
  const eyeParts = skin.parts.filter(p => p.id.startsWith("eyes_")).map(p => p.id);
  const mouthParts = skin.parts.filter(p => p.id.startsWith("mouth_")).map(p => p.id);

  // 思考气泡：三个圆，跟着头走（来源：SPEC「头边小气泡」「三个气泡」）
  const bubbles = document.createElement("div");
  bubbles.className = "bubbles";
  bubbles.innerHTML = "<i></i><i></i><i></i>";
  const bubs = [...bubbles.children];
  if (R.bubbleFill) bubbles.style.setProperty("--bub-fill", R.bubbleFill);   // 皮肤自己配气泡颜色（底色浅的皮肤白气泡看不清）
  if (R.bubbleEdge) bubbles.style.setProperty("--bub-edge", R.bubbleEdge);

  const nextBlink = (t) => t + 2 + Math.random() * 3;   // 来源：SPEC 随机眨眼 2–5 秒
  blinkAt = nextBlink(0);

  // 姿势 → rig 状态。t 秒；o.loop 给了就把所有周期取整到 loop 的整数分之一（录循环帧用）
  function st(t, o) {
    const f = (hz) => o.loop ? Math.max(1, Math.round(hz * o.loop)) / o.loop : hz;
    // 频率：呼吸约 3.6 秒一次、头慢摆 6–7 秒、发梢 4–5 秒（来源：Coopanion figure.js:568 breath sin(t·2.4)/2π≈0.38Hz 偏快，表盘放慢）
    const breath = Math.sin(TAU * f(0.28) * t);
    // 待机头摆：两个正弦叠加，峰值 1.8°（来源：SPEC 头 ±2°；Coopanion figure.js:572 sin(t·.9)·1.2）
    const idleTilt = 1.2 * Math.sin(TAU * f(0.15) * t) + 0.6 * Math.sin(TAU * f(0.41) * t + 1);
    const tilt = o.tilt + idleTilt;
    const ty = o.ty + 1.5 * breath;   // 吸气时头跟着上抬 1.5 像素（来源：Coopanion figure.js:681 neck ty = breath·.35，按母图比例放大）
    const hairAmp = R.hairAmp ?? 14;  // 发梢最大位移像素（来源：SPEC 发梢 ±2°：母图发长约 400 像素 × tan2° ≈ 14）
    const hL = o.hairL ?? Math.sin(TAU * f(0.22) * t + 0.6), hR = o.hairR ?? Math.sin(TAU * f(0.22) * t + 1.4);
    const s = {
      all: { sx: 1 + 0.003 * breath, sy: 1 + 0.006 * breath },   // 来源：SPEC 呼吸缩放 ±0.6%（横向减半，同 Coopanion figure.js:653）
      head: {
        fn: (u, v, x, y) => {
          const w = headW(x, y);
          if (w <= 0) return [0, 0];
          const a = tilt * w * Math.PI / 180, c = Math.cos(a), sn = Math.sin(a), dx = x - ncx, dy = y - ncy;
          return [dx * c - dy * sn - dx, dx * sn + dy * c - dy + ty * w];
        },
      },
      hair: {
        fn: (u, v, x, y) => {
          let d = 0;
          for (const h of R.hair || []) { const w = hairW(x, y, h); if (w > 0) d += w * hairAmp * (h[4] === "L" ? hL : hR); }
          return [d, 0];
        },
      },
      alpha: {},
    };
    for (const id of eyeParts) s.alpha[id] = 0;
    for (const id of mouthParts) s.alpha[id] = 0;
    if (o.eyes) for (const [id, k] of Object.entries(o.eyes)) s.alpha[id] = k;
    if (o.blink) s.alpha.eyes_closed = 1;
    if (o.mouth) s.alpha[o.mouth] = 1;
    return s;
  }

  // 录帧用：确定性姿势，不走弹簧
  function pose(name, t, o = {}) {
    const p = POSE[name] || POSE.idle;
    const s = st(t, { loop: o.loop, tilt: p.tilt, ty: p.ty, eyes: p.eyes ? { [p.eyes]: 1 } : {}, blink: o.blink, mouth: o.mouth });
    placeBubbles(s, name === "thinking" ? 1 : 0, t, o.loop);
    return s;
  }

  function placeBubbles(s, k, t, loop) {
    bubbleK = k;
    if (!R.bubble) return;
    const [bx, by] = R.bubble;
    const q = rig.point("hair", s, bx, by);
    const v = model.view;
    bubbles.style.left = ((q[0] - v[0]) / (v[2] - v[0]) * 100).toFixed(2) + "%";
    bubbles.style.top = ((q[1] - v[1]) / (v[3] - v[1]) * 100).toFixed(2) + "%";
    bubbles.style.opacity = k.toFixed(3);
    // 三个气泡依次浮起、轮流亮，约 0.9 秒一轮（来源：旧版表盘 .dots 动画 1.1s 周期，略快）
    const hz = loop ? Math.max(1, Math.round(1.1 * loop)) / loop : 1.1;
    bubs.forEach((b, i) => {
      const ph = ((t * hz - i * 0.22) % 1 + 1) % 1;
      const out = bx < (model.view[0] + model.view[2]) / 2 ? -1 : 1;   // 气泡往头外侧飘：锚点在左半边就往左
      b.style.transform = `translate(${out * i * 0.6}em, ${-i * 1.3 - 0.25 * Math.sin(TAU * ph)}em) scale(${(0.55 + i * 0.25) * (0.9 + 0.1 * Math.sin(TAU * ph))})`;
      b.style.opacity = (0.65 + 0.35 * Math.sin(TAU * ph)).toFixed(3);
    });
  }

  // 拨片：三档嘴（闭/半张/张开），带回差不抖。门槛来源：实测 macOS say 合成一句中文的包络（5.25 秒 105 格），
  // 0.18 以下约 29%（字间停顿）、0.55 以上约 50%，三档都常出现
  function mouthFromLevel(lv) {
    const half = mouthParts.includes("mouth_half") ? "mouth_half" : "mouth_open";
    if (mouthSprite === "mouth_open") return lv > 0.42 ? "mouth_open" : lv > 0.12 ? half : "";
    if (mouthSprite) return lv > 0.55 ? "mouth_open" : lv > 0.08 ? half : "";
    return lv > 0.55 ? "mouth_open" : lv > 0.18 ? half : "";
  }

  function level(t) {
    if (state !== "speaking") return 0;
    if (env && env.length) {
      const i = Math.floor((t - envT0) * 1000 / envMs);
      if (i < env.length) return env[Math.max(0, i)];
    }
    // 没有音量包络（或放完了还在说）：按说话音节节奏合成，约每秒 4 个字（来源：实测同一句 19 字 5.25 秒≈3.6 字/秒）
    return clamp(0.5 + 0.5 * Math.sin(t * 13.5) * (0.6 + 0.4 * Math.sin(t * 3.1)), 0, 1) * (0.7 + 0.3 * Math.sin(t * 0.9));
  }

  function frame() {
    raf = requestAnimationFrame(frame);
    // 待机时隔帧画，约 30 帧，省电（来源：交接「低性能机器上 CPU 占用（静止时降帧）」）
    if (state === "idle" && ++idleSkip % 2) return;
    const now = performance.now() / 1000;
    const dt = Math.min(0.1, now - last); last = now;
    const t = now - t0;
    const p = POSE[state] || POSE.idle;
    const k = ease(SWITCH_RATE, dt);
    for (const id of eyeParts) eyeK[id] = lerp(eyeK[id] || 0, id === p.eyes ? 1 : 0, k);
    mouthLvl = lerp(mouthLvl, level(t), ease(30, dt));   // 来源：包络 50ms 一格，30/s 平滑约 1.5 格，不拖嘴
    mouthSprite = mouthFromLevel(mouthLvl);
    // 闭眼 0.12 秒；15% 概率紧跟一次连眨（来源：Coopanion kit/body.js 眨眼时长量级，人眨眼 0.1–0.15 秒）
    if (t > blinkAt) { blinkEnd = t + 0.12; blinkAt = nextBlink(t); if (Math.random() < 0.15) blinkAt = t + 0.35; }
    // 说话时头轻点：0.8°、约 0.43Hz（来源：SPEC 头 ±2° 内）
    const tilt = sp.tilt.step(p.tilt + (state === "speaking" ? 0.8 * Math.sin(t * 2.7) : 0), dt);
    const ty = sp.ty.step(p.ty - (state === "speaking" ? 2.5 * mouthLvl : 0), dt);   // 张嘴时下巴微抬 2.5 像素（来源：同上）
    const lag = sp.tilt.v * 0.25;   // 头一转发梢往反方向甩一下（来源：Coopanion figure.js:613 ahoge 读 -tiltVel·.12，发梢更长取 .25）
    const hairL = sp.hairL.step(Math.sin(TAU * 0.22 * t + 0.6) - lag, dt), hairR = sp.hairR.step(Math.sin(TAU * 0.22 * t + 1.4) - lag, dt);
    const s = st(t, { tilt, ty, eyes: { ...eyeK }, blink: t < blinkEnd, mouth: mouthSprite, hairL, hairR });
    placeBubbles(s, lerp(bubbleK, state === "thinking" ? 1 : 0, k), t);
    rig.render(s);
  }

  screen.prepend(bubbles);
  screen.prepend(canvas);
  if (!opts.manual) raf = requestAnimationFrame(frame);

  return {
    canvas, skin,
    set(next, o = {}) {
      if (next === "speaking" && o.env) { env = o.env; envMs = o.envMs || ENV_MS; envT0 = performance.now() / 1000 - t0; }
      if (next !== "speaking") env = null;
      state = POSE[next] ? next : "idle";
    },
    pose, draw(s) { rig.render(s); },
    stop() { cancelAnimationFrame(raf); raf = 0; },   // 录帧时停掉实时循环，改由 pose()+draw() 一帧帧画
    // 静止帧：不变形、不贴差分，应和母图逐像素一致（tools/dial_frames.py --check 用它核对）
    rest() { return { alpha: Object.fromEntries([...eyeParts, ...mouthParts].map(id => [id, 0])) }; },
    dispose() { cancelAnimationFrame(raf); rig.dispose(); canvas.remove(); bubbles.remove(); },
  };
}
