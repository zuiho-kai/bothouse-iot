"""本机 Qwen-Image 局部重画（ComfyUI，127.0.0.1:8188）：python tools/skin/qwen_inpaint.py <crop.png> <mask.png> <out.png> "<prompt>" [denoise] [seed]
crop 和 mask 同尺寸（mask 白=重画）。走本机 ComfyUI 8188。"""
import json, sys, time, uuid, urllib.request, urllib.parse
from pathlib import Path
H = "http://127.0.0.1:8188"
def up(p):
    b = uuid.uuid4().hex; data = Path(p).read_bytes()
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{Path(p).name}\"\r\nContent-Type: image/png\r\n\r\n").encode() + data + \
           f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{b}--\r\n".encode()
    r = urllib.request.Request(H + "/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    return json.load(urllib.request.urlopen(r))["name"]
crop, mask, out, prompt = sys.argv[1:5]
den = float(sys.argv[5]) if len(sys.argv) > 5 else 0.85
seed = int(sys.argv[6]) if len(sys.argv) > 6 else 7
ci, mi = up(crop), up(mask)
neg = "blurry, lowres, deformed, extra eyes, realistic, 3d"
wf = {
 "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_bf16.safetensors", "weight_dtype": "default"}},
 "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_bf16.safetensors", "type": "qwen_image", "device": "default"}},
 "3": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
 "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "prompt": prompt, "negative_prompt": neg, "resolution": 1024}},
 "6": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
 "10": {"class_type": "LoadImage", "inputs": {"image": ci}},
 "11": {"class_type": "LoadImageMask", "inputs": {"image": mi, "channel": "red"}},
 "12": {"class_type": "VAEEncode", "inputs": {"pixels": ["10", 0], "vae": ["3", 0]}},
 "13": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["12", 0], "mask": ["11", 0]}},
 "7": {"class_type": "KSampler", "inputs": {"model": ["6", 0], "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["13", 0],
        "seed": seed, "steps": 25, "cfg": 1, "sampler_name": "euler", "scheduler": "simple", "denoise": den}},
 "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
 "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "dialanim"}},
}
r = urllib.request.Request(H + "/prompt", json.dumps({"prompt": wf}).encode(), {"Content-Type": "application/json"})
pid = json.load(urllib.request.urlopen(r))["prompt_id"]
t0 = time.time()
while True:
    h = json.load(urllib.request.urlopen(f"{H}/history/{pid}"))
    if pid in h:
        st = h[pid].get("status", {})
        if st.get("status_str") == "error": sys.exit(json.dumps(st)[:2000])
        o = h[pid]["outputs"]["9"]["images"][0]
        q = urllib.parse.urlencode({"filename": o["filename"], "subfolder": o["subfolder"], "type": o["type"]})
        Path(out).write_bytes(urllib.request.urlopen(f"{H}/view?{q}").read()); break
    if time.time() - t0 > 900: sys.exit("timeout")
    time.sleep(2)
print(out, f"{time.time()-t0:.0f}s")
