"""会动的表盘：皮肤目录、引擎脚本、音量包络、给板子的帧包。"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_remote_dial_release import DEV, cfg, read_sse  # noqa: E402

from bothouse_iot.app import App  # noqa: E402
from bothouse_iot.config import load  # noqa: E402
from bothouse_iot.dial import Dial  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


async def started(tmp_path, board=""):
    a = App(load(cfg(tmp_path, board=board)), log=lambda s: None)
    await a.xz.start()
    return a


def make_skins(tmp_path):
    d = tmp_path / "skins"
    for name in ("standard", "pastel"):
        (d / name).mkdir(parents=True)
        (d / name / "skin.json").write_text(json.dumps({"size": [8, 8], "textures": {}, "parts": [], "rig": {}}))
        (d / name / "base.png").write_bytes(b"png")
    (d / "pastel" / "frames-240.bin").write_bytes(b"RHA1" + b"\0" * 8)
    (d / "notaskin").mkdir()
    return d


async def test_skins_listed_default_first_and_served(tmp_path):
    make_skins(tmp_path)
    a = await started(tmp_path, 'skins = "skins"\nskin = "pastel"')
    base = f"http://127.0.0.1:{a.xz.port}"
    try:
        assert a.dial.skins("ju") == ["pastel", "standard"]
        async with aiohttp.ClientSession() as http:
            async with http.get(base + "/dial/events") as r:
                m = (await read_sse(r, 1))[0]
                assert m["skins"] == ["pastel", "standard"] and m["skin_base"] == "dial/skin/ju/"
            async with http.get(base + "/dial/skin/ju/standard/skin.json") as r:
                assert r.status == 200 and (await r.json())["size"] == [8, 8]
            for bad in ("/dial/skin/ju/notaskin/skin.json", "/dial/skin/ju/standard/..%2Fpastel%2Fskin.json",
                        "/dial/skin/nobody/standard/skin.json", "/dial/skin/ju/standard/missing.png"):
                async with http.get(base + bad) as r:
                    assert r.status == 404, bad
            async with http.get(base + "/dial/static/dial-figure.js") as r:
                assert r.status == 200 and "createRig" in await r.text()
            async with http.get(base + "/dial/static/vendor/coopanion/rig.js") as r:
                assert r.status == 200 and "export function createRig" in await r.text()
            async with http.get(base + "/dial/static/vendor/coopanion/LICENSE") as r:
                assert r.status == 200
            async with http.get(base + "/dial/static/..%2F..%2Fconfig.py") as r:
                assert r.status == 404
            # 板子：默认皮肤录了 240 的帧包才发 anim；别的尺寸 404，板子留着静态头像
            assert a.dial.message("ju", "http://h")["anim"] == "http://h/xiaozhi/dial/ju.anim"
            async with http.get(base + "/xiaozhi/dial/ju.anim?size=240") as r:
                assert r.status == 200 and (await r.read())[:4] == b"RHA1"
            async with http.get(base + "/xiaozhi/dial/ju.anim?size=360") as r:
                assert r.status == 404
    finally:
        await a.xz.close()


async def test_no_skins_no_anim(tmp_path):
    a = await started(tmp_path)
    try:
        assert a.dial.skins("ju") == []
        assert "anim" not in a.dial.message("ju", "http://h")
        assert a.dial.page_message("ju")["skin_base"] == ""
    finally:
        await a.xz.close()


async def test_speaking_carries_envelope_and_new_line_repushes(tmp_path):
    a = await started(tmp_path)
    base = f"http://127.0.0.1:{a.xz.port}"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(base + "/dial/events") as r:
                await read_sse(r, 1)
                a.dial.set("ju", "speaking", [0.1, 1.0])
                a.dial.set("ju", "speaking", [0.5, 0.2, 0.9])   # 下一句：照推
                a.dial.set("ju", "speaking")                     # 同一状态没新包络：不推
                a.dial.set("ju", "idle")
                got = await read_sse(r, 3)
                assert got[0]["env"] == [0.1, 1.0] and got[0]["env_ms"] == 50
                assert got[1]["env"] == [0.5, 0.2, 0.9]
                assert got[2]["state"] == "idle" and "env" not in got[2]
    finally:
        await a.xz.close()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="要 ffmpeg")
def test_envelope_follows_loudness(tmp_path):
    wav = tmp_path / "t.wav"
    # 0.5 秒静音 + 0.5 秒 440Hz
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=16000", "-filter_complex",
                    "[0]atrim=0:0.5[a];[1]atrim=0:0.5[b];[a][b]concat=n=2:v=0:a=1", str(wav)], check=True)
    env = Dial.envelope(wav.read_bytes())
    assert 18 <= len(env) <= 22
    assert max(env[:9]) < 0.05 and min(env[11:19]) > 0.8
    assert Dial.envelope(b"not audio") == []


def test_repo_skins_are_complete():
    """仓里自带的橘两款皮肤：skin.json 引用的贴图都在，差分贴片在母图范围内。"""
    for d in sorted((ROOT / "skins" / "ju").iterdir()):
        s = json.loads((d / "skin.json").read_text())
        W, H = s["size"]
        assert set(s["textures"]) >= {"base", "eyes_closed", "eyes_think", "mouth_open", "mouth_half"}, d.name
        for t in s["textures"].values():
            assert (d / t).is_file(), (d.name, t)
        for p in s["parts"]:
            x, y, w, h = p["box"]
            assert 0 <= x and 0 <= y and x + w <= W and y + h <= H, (d.name, p["id"])
        assert all(k in s["rig"] for k in ("neck", "head", "hair", "bubble"))
