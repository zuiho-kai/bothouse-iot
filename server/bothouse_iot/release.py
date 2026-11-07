"""从 GitHub Release 取最新固件给板子 Wi-Fi 升级（配置 [device] firmware_release = "<owner>/<repo>"）。

    启动时和之后每 CHECK_SEC 秒查一次 <repo> 的最新正式 Release，有 xiaozhi-<板型>.bin 就下载到 data_dir/firmware/，
    对上 Release 里 SHA256SUMS 的校验和才用；换上以后 XiaozhiServer 照旧按 elf_sha256 比较、让不一样的板子升级
    （xiaozhi.py _check_firmware：只在空闲时发、同一块板对同一个文件只发一次）。
    Release 产物由 .github/workflows/firmware.yml 打 tag 时编出，不含 NVS（Wi-Fi、钥匙、OTA 地址）。
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Callable

import aiohttp

API = "https://api.github.com"
CHECK_SEC = 6 * 3600          # 多久查一次新 Release（固件发版不频繁；查一次一个 API 请求）
DEFAULT_ASSET = "xiaozhi-spotpear-sp-esp32-s3-1.28-box.bin"


class ReleaseFirmware:
    def __init__(self, repo: str, asset: str, dest: Path, on_new: Callable[[Path, str], None],
                 log: Callable[[str], None] = print, api: str = API):
        self.repo, self.asset, self.dest, self.on_new, self.log, self.api = repo, asset, dest, on_new, log, api.rstrip("/")
        self.tag: str | None = None

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.check()
            try:
                await asyncio.wait_for(stop.wait(), CHECK_SEC)
            except asyncio.TimeoutError:
                pass

    async def check(self) -> Path | None:
        """查一次；有新固件就下载校验并回调 on_new(路径, tag)。失败只记一行，下次再查。"""
        try:
            async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=300)) as http:
                async with http.get(f"{self.api}/repos/{self.repo}/releases/latest",
                                    headers={"Accept": "application/vnd.github+json"}) as r:
                    if r.status == 404:
                        self.log(f"[firmware] {self.repo} 还没有正式 Release，先不升级")
                        return None
                    r.raise_for_status()
                    rel = await r.json()
                tag = str(rel.get("tag_name") or "")
                if tag == self.tag:
                    return None
                assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets") or []}
                url = assets.get(self.asset)
                if not url:
                    self.log(f"[firmware] {self.repo} {tag} 里没有 {self.asset}，不升级")
                    return None
                path = self.dest / f"{tag}-{self.asset}"
                if not path.is_file():
                    async with http.get(url) as r:
                        r.raise_for_status()
                        data = await r.read()
                    want = None
                    if assets.get("SHA256SUMS"):
                        async with http.get(assets["SHA256SUMS"]) as r:
                            r.raise_for_status()
                            for line in (await r.text()).splitlines():
                                parts = line.split()
                                if len(parts) == 2 and parts[1].lstrip("*") == self.asset:
                                    want = parts[0].lower()
                    have = hashlib.sha256(data).hexdigest()
                    if want is not None and want != have:
                        self.log(f"[firmware] {self.asset}（{tag}）校验和不对：{have[:12]} ≠ {want[:12]}，不用")
                        return None
                    self.dest.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(data)
                    tmp.replace(path)
                    self.log(f"[firmware] 下载了 {self.repo} {tag} 的 {self.asset}（{len(data)} 字节"
                             f"{'，校验和对上' if want else '，Release 里没有 SHA256SUMS，没校验'}）")
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as e:
            self.log(f"[firmware] 查 {self.repo} 的 Release 失败：{e!r}，{CHECK_SEC // 3600} 小时后再查")
            return None
        self.tag = tag
        self.on_new(path, tag)
        return path
