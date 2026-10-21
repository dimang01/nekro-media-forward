"""Public-video forwarding for OneBot V11 / NapCat.

抖音的 web 接口现在被 Argus 风控保护：任何纯 HTTP 客户端（yt-dlp / curl）即使带上
浏览器 cookie，也会拿到 403 `Uifid Not Found` 或空响应；而且这套风控还会按 IP 信誉
拦截机房 IP。可用的做法只有一个——让真实浏览器打开视频页，由页面自己签名请求，再把
拿到的 CDN 直链交给服务器下载。

因此本插件把抖音拆成两步：

1. **解析**：交给跑在住宅网络里的本地解析器（``media-resolver``，使用无头 Edge +
   CDP）。它通过本插件的 ``/resolver/*`` 接口领取任务并回传直链——本机在 NAT 后面，
   所以由它主动轮询，服务器不需要能连回本地。
2. **下载与转发**：服务器（腾讯云）直接抓 CDN 直链完全没有问题，下载后仍旧通过短时
   链接交给本机 NapCat 以合并转发发出。

小红书继续走 yt-dlp。
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import hashlib
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from nonebot import get_bot
from pydantic import BaseModel, ConfigDict, Field

from nekro_agent.api.message import ChatMessage
from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.api.signal import MsgSignal


plugin = NekroPlugin(
    name="短视频合并转发",
    module_name="video_forward",
    description="收到公开的抖音 / 小红书 / TikTok / B站 视频链接后，将视频以 QQ 合并转发发回原聊天。抖音与小红书由本地解析器取直链，B站走公开 API。",
    version="0.9.0",
    author="local_video",
    url="https://github.com/KroMiose/nekro-agent",
    support_adapter=["onebot_v11"],
    allow_sleep=False,
)


@plugin.mount_config()
class VideoForwardConfig(ConfigBase):
    """公开短视频下载与合并转发设置。受保护、付费、仅好友可见或需要登录的视频不会被绕过。"""

    public_base_url: str = Field(
        default="http://127.0.0.1:8022",
        title="NekroAgent 公网地址",
        description="NapCat 用来取得视频临时链接的地址（填你自己的服务器，不要带 /webui 后缀）。",
    )
    max_file_size_mb: int = Field(
        default=60,
        ge=5,
        le=200,
        title="单个视频最大体积（MB）",
        description="超过此大小不下载，避免占满云盘或让 QQ 上传失败。",
    )
    expiry_minutes: int = Field(
        default=30,
        ge=5,
        le=120,
        title="临时视频链接有效期（分钟）",
        description="QQ 上传完成后即可失效；默认 30 分钟。",
    )
    cooldown_seconds: int = Field(
        default=15,
        ge=0,
        le=120,
        title="同一聊天冷却时间（秒）",
        description="防止同一聊天连续发链接导致重复下载。",
    )
    yt_dlp_path: str = Field(
        default="/usr/local/bin/yt-dlp",
        title="yt-dlp 命令位置",
        description="保持默认即可；服务器找不到 yt-dlp 时会给出明确提示。",
    )
    resolver_enabled: bool = Field(
        default=True,
        title="启用本地解析器（抖音 / 小红书 / TikTok；B站不需要）",
        description=(
            "抖音和小红书的 web 接口有风控且会拦截机房 IP，必须由住宅网络里的本地解析器"
            "（media-resolver）用真实浏览器取直链；TikTok 在大陆被墙、云端完全不可达，"
            "同样由本机解析器走代理下载后直接把文件路径交给 NapCat。"
            "关闭后这三个平台会回退到 yt-dlp（基本一定失败）。"
        ),
    )
    resolver_token: str = Field(
        default="",
        title="本地解析器通信密钥",
        description=(
            "本地解析器与服务器之间的共享密钥，必须与 media-resolver 的 config.json 一致；"
            "留空表示禁用解析器接口。"
        ),
    )
    resolver_timeout: int = Field(
        default=150,
        ge=30,
        le=600,
        title="等待本地解析的超时时间（秒）",
        description="本地解析器要启动无头浏览器并等待页面返回详情，通常 10~40 秒，网络慢时请调大。",
    )
    resolver_stale_seconds: int = Field(
        default=60,
        ge=15,
        le=600,
        title="解析器离线判定（秒）",
        description="超过这么久没有收到本地解析器的心跳，就认为它没在运行。",
    )
    send_timeout: int = Field(
        default=300,
        ge=30,
        le=1800,
        title="合并转发超时（秒）",
        description=(
            "NapCat 要把视频上传到 QQ 服务器才返回，家用宽带上行较慢时需要放宽；"
            "默认 300 秒。"
        ),
    )
    max_images: int = Field(
        default=9,
        ge=1,
        le=20,
        title="图文笔记最多发送张数",
        description=(
            "小红书/抖音的图文笔记（没有视频流）会按顺序下载并合并转发其图片；"
            "超过这个数量只发前几张，避免刷屏。默认 9 张。"
        ),
    )
    allow_resolver_local_path: bool = Field(
        default=False,
        title="允许直接用解析器的本机路径",
        description=(
            "NapCat 与 media-resolver 在同一台机器上时打开（例如 Windows 上同时跑 QQ/NapCat 和解析器）："
            "TikTok 这类由解析器下载好的文件直接把本机路径交给 NapCat，服务器不中转、更快。"
            "NapCat 上云后必须关闭（云端读不到本机路径），改由解析器上传文件。"
        ),
    )
    forward_nickname: str = Field(
        default="机器人",
        title="合并转发显示的昵称",
        description="合并转发卡片里显示的发送者昵称，默认「机器人」。",
    )
    bilibili_max_size_mb: int = Field(
        default=300,
        ge=10,
        le=1500,
        title="B站视频体积上限（MB）",
        description=(
            "B 站视频普遍比短视频大，单独给一个上限（默认 300MB）。"
            "超过上限不会发送，并会提示视频的实际大小。"
        ),
    )
    bilibili_cookie: str = Field(
        default="",
        title="B站 Cookie（可选，用于登录态）",
        description=(
            "留空 = 匿名访问（B 站只给到 720p，会员/登录可见视频会失败）。"
            "填浏览器里 bilibili.com 的 SESSDATA 值即可（也可以粘整行 Cookie）。"
            "注意：这相当于把账号登录态交给服务器上的插件，请自行评估风险，别用主账号。"
        ),
    )
    bilibili_ffmpeg_path: str = Field(
        default="",
        title="B站 ffmpeg 路径（可选）",
        description=(
            "合并 DASH 音视频需要 ffmpeg（1080p / 4K 必须走这条路）。"
            "留空时自动查找 /home/ubuntu/srv/nekro_agent/bin/ffmpeg，再找 PATH 里的 ffmpeg；"
            "找不到就自动退回 720p 单文件 mp4。"
        ),
    )


_SUPPORTED_DOMAINS = (
    "douyin.com",
    "iesdouyin.com",
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",
    "tiktok.com",
    "bilibili.com",
    "b23.tv",
)
# 需要本地解析器处理的平台
# 抖音 / 小红书：风控 + 机房 IP 拦截，必须住宅 IP + 真实浏览器取直链
# TikTok：大陆直连被 DNS 污染，云端完全不可达，只能在本机走代理下载
_RESOLVER_DOMAINS = (
    "douyin.com",
    "iesdouyin.com",
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",
    "tiktok.com",
)
_DOWNLOAD_REFERER = {
    "douyin": "https://www.douyin.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "tiktok": "https://www.tiktok.com/",
    "bilibili": "https://www.bilibili.com/",
}
# QQ 分享文案有时会把短链和标题拆成不同消息段，甚至省去 https://。
# 因此只找受支持站点，而不是依赖整段消息恰好是一个标准 URL。
_URL_RE = re.compile(
    r"(?:(?:https?://)?(?:[a-z0-9-]+\.)*(?:douyin\.com|iesdouyin\.com|"
    r"xiaohongshu\.com|xhslink\.(?:com|cn)|tiktok\.com|bilibili\.com|b23\.tv)"
    r"(?:/[^\s<>'\"，。！？、]*)?)",
    re.IGNORECASE,
)
_VIDEO_DIR = Path("/tmp/nekro-video-forward")
_LAST_CHAT_REQUEST: dict[str, float] = {}
_RECENT_URLS: dict[tuple[str, str], float] = {}
_IN_FLIGHT: set[tuple[str, str]] = set()

_DOWNLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)


_PLATFORM_NAMES = {"douyin": "抖音", "xiaohongshu": "小红书", "tiktok": "TikTok", "bilibili": "B站"}


@dataclass
class _MediaSource:
    """一次待发送的媒体。

    要么 ``path`` 指向服务器上的文件（经临时链接交给 NapCat 下载），
    要么 ``local_path`` 是 NapCat 所在机器上的文件路径（TikTok 就是这种，
    因为大陆/云端都访问不到 TikTok CDN）。
    """

    name: str
    path: Optional[Path] = None
    local_path: Optional[str] = None
    size: int = 0
    # 图文笔记（小红书/抖音图集）：图片直链，由插件逐张下载后合成一条合并转发。
    # 非空时 path / local_path 均为空。
    images: tuple = ()


@dataclass
class _Lease:
    path: Path
    expires_at: float
    download_name: str


_LEASES: dict[str, _Lease] = {}

# 解析器主动上传到服务器的文件（job_id -> (路径, 过期时间)）。
# QQ/NapCat 上云后，解析器回传的 local_path 在服务器上并不存在，
# 所以由解析器先把文件 POST 上来，插件再用 /media 临时链接交付。
_UPLOADS: Dict[str, tuple[Path, float]] = {}
_UPLOAD_TTL_SECONDS = 900


# ---------------------------------------------------------------- 本地解析器队列


@dataclass
class _ResolveJob:
    """等待本地解析器回传结果的任务。"""

    id: str
    url: str
    created_at: float
    future: "asyncio.Future[Dict[str, Any]]"
    delivered_at: Optional[float] = None


_JOBS: Dict[str, _ResolveJob] = {}
_WAITERS: set["asyncio.Future[None]"] = set()
_LAST_POLL: float = 0.0
_STATS: Dict[str, int] = {"poll": 0, "jobs": 0, "ok": 0, "failed": 0}


def _resolver_token() -> str:
    return str(plugin.get_config().resolver_token or "").strip()


def _resolver_online() -> bool:
    if not _resolver_token():
        return False
    now = time.time()
    if (now - _LAST_POLL) < int(plugin.get_config().resolver_stale_seconds):
        return True
    # 本地解析器正在处理任务时不会发心跳（轮询循环被占用），这段时间不能判它离线，
    # 否则解析要几十秒的视频会收到「解析器未连接」这种误导提示。
    grace = int(plugin.get_config().resolver_timeout)
    return any(
        job.delivered_at is not None and (now - job.delivered_at) < grace
        for job in _JOBS.values()
    )


def _resolver_authorized(token: Optional[str]) -> bool:
    expected = _resolver_token()
    if not expected:
        return False
    return secrets.compare_digest(str(token or ""), expected)


def _wake_waiters() -> None:
    for waiter in list(_WAITERS):
        if not waiter.done():
            waiter.set_result(None)


def _next_pending_job() -> Optional[_ResolveJob]:
    """取出最早的未派发任务（超过 45 秒未回传的任务允许重新派发）。"""
    now = time.time()
    for job in sorted(_JOBS.values(), key=lambda item: item.created_at):
        if job.delivered_at and now - job.delivered_at < 45:
            continue
        job.delivered_at = now
        return job
    return None


async def _resolve_via_local(url: str) -> Dict[str, Any]:
    """把抖音链接交给本地解析器，等待它回传 CDN 直链。"""
    loop = asyncio.get_running_loop()
    job = _ResolveJob(
        id=uuid.uuid4().hex,
        url=url,
        created_at=time.time(),
        future=loop.create_future(),
    )
    _JOBS[job.id] = job
    _STATS["jobs"] += 1
    _wake_waiters()
    try:
        return await asyncio.wait_for(job.future, timeout=int(plugin.get_config().resolver_timeout))
    except asyncio.TimeoutError as error:
        _STATS["failed"] += 1
        raise RuntimeError(
            "本地解析器没有在限定时间内返回结果，请确认 Windows 上的 media-resolver 正在运行。"
        ) from error
    finally:
        _JOBS.pop(job.id, None)


# ---------------------------------------------------------------- 基础工具


def _find_supported_url(text: str) -> Optional[str]:
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?)]}，。！？")
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        host = (urlparse(url).hostname or "").lower().strip(".")
        if any(host == domain or host.endswith("." + domain) for domain in _SUPPORTED_DOMAINS):
            return url
    return None


def _find_message_url(message: ChatMessage) -> Optional[str]:
    """Read ordinary text first, then QQ/NapCat's raw message representation."""
    candidates = [getattr(message, "content_text", "")]
    for attribute in ("raw_message", "message", "original_message"):
        value = getattr(message, attribute, "")
        if value:
            candidates.append(str(value))
    candidates.append(str(message))
    for text in candidates:
        url = _find_supported_url(text)
        if url:
            return url
    return None


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _platform_of(url: str) -> Optional[str]:
    """判断是否属于需要本地解析器处理的平台，返回 douyin / xiaohongshu / tiktok。"""
    host = _host_of(url)
    if "tiktok" in host and (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return "tiktok"
    if any(host == domain or host.endswith("." + domain) for domain in _RESOLVER_DOMAINS):
        return "douyin" if "douyin" in host else "xiaohongshu"
    return None


def _needs_local_resolver(url: str) -> bool:
    return _platform_of(url) is not None


def _source_name(url: str) -> str:
    host = _host_of(url)
    if "douyin" in host:
        return "抖音"
    if "xiaohongshu" in host or "xhslink" in host:
        return "小红书"
    if "tiktok" in host:
        return "TikTok"
    if "bilibili" in host or "b23.tv" in host:
        return "B站"
    return "未知平台"


def _looks_like_local_path(value: str) -> bool:
    """本地解析器回传的 NapCat 本机文件路径（Windows 盘符 / file:// / POSIX 绝对路径）。"""
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith("file://"):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return True
    return text.startswith("/")


def _take_upload(job_id: str) -> Optional[Path]:
    """取出某任务上传的文件（取出即从登记表移除，后续生命周期交给 media lease）。"""
    if not job_id:
        return None
    entry = _UPLOADS.pop(job_id, None)
    if entry is None:
        return None
    path, _expires_at = entry
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def _cleanup_expired() -> None:
    now = time.time()
    for token, lease in list(_LEASES.items()):
        if lease.expires_at > now:
            continue
        try:
            lease.path.unlink(missing_ok=True)
            parent = lease.path.parent
            parent.rmdir()
        except OSError:
            pass
        _LEASES.pop(token, None)
    for key, expires_at in list(_RECENT_URLS.items()):
        if expires_at <= now:
            _RECENT_URLS.pop(key, None)
    for job_id, (path, expires_at) in list(_UPLOADS.items()):
        if expires_at > now:
            continue
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            pass
        _UPLOADS.pop(job_id, None)


def _human_size(num_bytes: Optional[float]) -> str:
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "未知"
    if value <= 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return "未知"


def _pick_stream(videos: List[Dict[str, Any]], max_bytes: int) -> Optional[Dict[str, Any]]:
    """在本地解析器返回的多个清晰度里挑一个不超过体积上限、尽量清晰的。"""
    usable = [item for item in videos if isinstance(item, dict) and item.get("url")]
    if not usable:
        return None
    sized = [item for item in usable if item.get("size")]
    within = [item for item in sized if int(item["size"]) <= max_bytes]
    if within:
        return max(within, key=lambda item: int(item["size"]))
    if sized:
        return min(sized, key=lambda item: int(item["size"]))
    return usable[0]


# ---------------------------------------------------------------- 下载


async def _download_direct(url: str, target: Path, max_bytes: int, headers: Dict[str, str]) -> Path:
    """流式下载直链，超过体积上限立即中止。"""
    limit = int(max_bytes)
    total = 0
    timeout = httpx.Timeout(30.0, read=180.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared and declared > limit:
                raise RuntimeError(
                    f"视频体积 {_human_size(declared)} 超过 {_human_size(limit)} 上限，未下载。"
                )
            with target.open("wb") as handle:
                async for chunk in response.aiter_bytes(1 << 16):
                    total += len(chunk)
                    if total > limit:
                        raise RuntimeError(f"视频超过 {_human_size(limit)} 上限，已中止下载。")
                    handle.write(chunk)
    if total <= 0:
        raise RuntimeError("下载到的视频是空文件。")
    return target


async def _run_ytdlp(url: str, task_dir: Path) -> tuple[Path, str]:
    """用 yt-dlp 下载（小红书 / X 走这条路）。"""
    binary = (plugin.get_config().yt_dlp_path or "yt-dlp").strip()
    if not binary or ("/" not in binary and "\\" not in binary and not shutil.which(binary)):
        raise RuntimeError("服务器尚未安装 yt-dlp，管理员需要先安装后才能解析视频。")

    output = task_dir / "video.%(ext)s"
    max_mb = int(plugin.get_config().max_file_size_mb)
    command = [
        binary,
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--restrict-filenames",
        "--max-filesize",
        f"{max_mb}M",
        "--format",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "--output",
        str(output),
        url,
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=8 * 60)
    except asyncio.TimeoutError as error:
        process.kill()
        await process.communicate()
        raise RuntimeError("下载超时，已停止本次任务。") from error
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        safe_detail = detail[-1] if detail else "视频站点拒绝了下载请求"
        raise RuntimeError(f"无法取得该视频：{safe_detail[:220]}")

    files = [path for path in task_dir.iterdir() if path.is_file() and path.stat().st_size]
    if not files:
        raise RuntimeError("下载没有产生可发送的视频文件。")
    video = max(files, key=lambda path: path.stat().st_size)
    if video.stat().st_size > max_mb * 1024 * 1024:
        raise RuntimeError(f"视频超过 {max_mb} MB 限制，未发送。")
    return video, video.name


# ---------------------------------------------------------------- B 站
# 机房 IP 访问 bilibili 网页会被风控（HTTP 412），但公开 API 正常：
#   x/web-interface/view  → 标题 / cid / 时长 / 分P
#   x/player/playurl      → fnval=1 直接返回「单文件 mp4」直链，不需要 ffmpeg 合并音视频
# 所以 B 站不需要本地解析器，云端直连即可（未登录时 B 站只会给到 720p/480p）。

# B 站 wbi 签名：非签名接口（x/player/playurl）只返回匿名画质（最高 480p 的 DASH），
# 1080p / 4K 必须走 x/player/wbi/playurl 并带上 w_rid / wts 签名。
_BILI_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


class _BilibiliWbi:
    """wbi 签名器：用 nav 接口里的 img_key / sub_key 按固定表混排出 mixin_key。"""

    def __init__(self) -> None:
        self.mixin_key = ""

    def refresh(self, img_url: str, sub_url: str) -> str:
        img_key = str(img_url).rsplit("/", 1)[-1].split(".")[0]
        sub_key = str(sub_url).rsplit("/", 1)[-1].split(".")[0]
        raw = img_key + sub_key
        self.mixin_key = "".join(raw[i] for i in _BILI_MIXIN_TAB if i < len(raw))[:32]
        return self.mixin_key

    def sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        signed = {key: value for key, value in params.items() if value is not None}
        signed["wts"] = int(time.time())
        query = urlencode(sorted(signed.items()), safe="")
        query = re.sub(r"[!'()*]", "", query)
        signed["w_rid"] = hashlib.md5((query + self.mixin_key).encode()).hexdigest()
        return signed


_BILI_QUALITY_LABELS = {
    6: "240p",
    16: "360p",
    32: "480p",
    64: "720p",
    74: "720p60",
    80: "1080p",
    112: "1080p+",
    116: "1080p60",
    120: "4K",
    125: "HDR",
    126: "杜比视界",
}


def _is_bilibili(url: str) -> bool:
    host = _host_of(url)
    return any(host == domain or host.endswith("." + domain) for domain in ("bilibili.com", "b23.tv"))


def _bilibili_ids(url: str) -> Dict[str, Any]:
    """从链接里取出 bvid / aid / 分P 页码。"""
    match = re.search(r"(BV[0-9A-Za-z]{10})", url)
    bvid = match.group(1) if match else ""
    match_av = re.search(r"/av(\d+)", url, re.IGNORECASE)
    aid = match_av.group(1) if match_av else ""
    page = 1
    match_page = re.search(r"[?&]p=(\d+)", url)
    if match_page:
        try:
            page = max(1, int(match_page.group(1)))
        except ValueError:
            page = 1
    return {"bvid": bvid, "aid": aid, "page": page}


def _bilibili_ffmpeg() -> str:
    """找可用的 ffmpeg：配置值 → 挂载目录 → PATH。找不到就返回空串（自动退回 720p mp4）。"""
    candidates = [
        str(getattr(plugin.get_config(), "bilibili_ffmpeg_path", "") or "").strip(),
        "/home/ubuntu/srv/nekro_agent/bin/ffmpeg",
        "ffmpeg",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if candidate == "ffmpeg" or Path(candidate).exists():
            return candidate
    return ""


def _pick_dash_streams(videos: Any, audios: Any) -> tuple[Optional[dict], Optional[dict]]:
    """挑画质最高、且编码对 QQ 友好的视频流 + 码率最高的音频流。"""
    codec_order = {7: 0, 12: 1, 13: 2}  # AVC(h264) > HEVC > AV1，QQ 对 h264 兼容最好
    best_video: Optional[dict] = None
    best_key: Optional[tuple] = None
    for item in videos or []:
        if not item or not item.get("baseUrl"):
            continue
        try:
            quality = int(item.get("id") or 0)
        except (TypeError, ValueError):
            quality = 0
        try:
            codec = int(item.get("codecid") or 0)
        except (TypeError, ValueError):
            codec = 0
        key = (-quality, codec_order.get(codec, 9))
        if best_key is None or key < best_key:
            best_key, best_video = key, item
    best_audio: Optional[dict] = None
    for item in audios or []:
        if not item or not item.get("baseUrl"):
            continue
        if best_audio is None or int(item.get("bandwidth") or 0) > int(best_audio.get("bandwidth") or 0):
            best_audio = item
    return best_video, best_audio


async def _download_bilibili_dash(
    resolved: Dict[str, Any], task_dir: Path, max_bytes: int
) -> tuple[Path, str]:
    """下载 DASH 的视频流 + 音频流，再用 ffmpeg 无损合并成一个 mp4（1080p 及以上必须走这条路）。"""
    dash = resolved.get("dash") or {}
    video_url = str(dash.get("video_url") or "")
    audio_url = str(dash.get("audio_url") or "")
    if not video_url or not audio_url:
        raise RuntimeError("B站 DASH 流地址缺失")
    ffmpeg = _bilibili_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("没有可用的 ffmpeg（无法合并 DASH 音视频）")

    headers = {"User-Agent": _DOWNLOAD_UA, "Referer": "https://www.bilibili.com/", "Accept": "*/*"}
    video_file = await _download_direct(video_url, task_dir / "video.m4s", max_bytes, headers)
    audio_file = await _download_direct(audio_url, task_dir / "audio.m4s", max_bytes, headers)
    total = video_file.stat().st_size + audio_file.stat().st_size
    if total > max_bytes:
        raise RuntimeError(
            f"B站视频合并前总体积 {_human_size(total)} 超过 {_human_size(max_bytes)} 上限，未发送。"
        )

    output = task_dir / "video.mp4"
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video_file),
        "-i",
        str(audio_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("ffmpeg 合并超时（视频过大）") from None
    if process.returncode != 0 or not output.exists() or output.stat().st_size == 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"ffmpeg 合并失败：{detail[-1][:200] if detail else '未知错误'}")

    label = str((resolved.get("videos") or [{}])[0].get("label") or "bilibili")
    media_id = str(resolved.get("aweme_id") or "bilibili")
    plugin.logger.info(f"B站 DASH 合并完成：{label} 输出={_human_size(output.stat().st_size)}")
    return output, f"B站-{label}-{media_id}.mp4"


async def _expand_bilibili_url(url: str) -> str:
    """b23.tv 短链 → 跟随跳转拿完整链接。"""
    if "b23.tv" not in _host_of(url):
        return url
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        response = await client.get(
            url, headers={"User-Agent": _DOWNLOAD_UA, "Referer": "https://www.bilibili.com/"}
        )
        return str(response.url)


async def _resolve_bilibili(url: str, force_mp4: bool = False) -> Dict[str, Any]:
    """取 B 站视频的元信息 + 单文件 mp4 直链（不需要登录、不需要 ffmpeg）。"""
    cookie = str(getattr(plugin.get_config(), "bilibili_cookie", "") or "").strip()
    headers = {"User-Agent": _DOWNLOAD_UA, "Referer": "https://www.bilibili.com/"}
    if cookie:
        # 允许只填 SESSDATA 的值，也允许直接粘整行 Cookie
        headers["Cookie"] = cookie if "=" in cookie else f"SESSDATA={cookie}"
    try:
        expanded = await _expand_bilibili_url(url)
    except Exception as error:  # noqa: BLE001 - 短链跳转失败时退回原链接
        plugin.logger.warning(f"B站短链展开失败（使用原链接继续）：{error}")
        expanded = url

    ids = _bilibili_ids(expanded)
    if not ids["bvid"] and not ids["aid"]:
        if re.search(r"/(bangumi|cheese)/play/", expanded):
            return {
                "ok": False,
                "platform": "bilibili",
                "error": "番剧 / 课程需要登录 Cookie，本插件暂不支持，请发普通投稿视频链接。",
            }
        return {
            "ok": False,
            "platform": "bilibili",
            "error": f"没能从链接里认出 BV 号（链接：{expanded[:100]}）。",
        }

    params = {"bvid": ids["bvid"]} if ids["bvid"] else {"aid": ids["aid"]}
    async with httpx.AsyncClient(headers=headers, timeout=25) as client:
        view = await client.get("https://api.bilibili.com/x/web-interface/view", params=params)
        view.raise_for_status()
        payload = view.json()
        if payload.get("code") != 0:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": f"B站接口返回 {payload.get('code')}：{payload.get('message')}",
            }
        data = payload.get("data") or {}
        pages = data.get("pages") or []
        page_index = min(max(1, ids["page"]), len(pages) or 1) - 1
        page_info = pages[page_index] if pages else {}
        cid = page_info.get("cid")
        if not cid:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": "B站接口没有返回 cid（视频可能已删除或仅会员可见）。",
            }

        # 有 ffmpeg 就走 DASH（1080p/4K）；否则退回单文件 mp4（匿名最高 720p）
        ffmpeg_path = "" if force_mp4 else _bilibili_ffmpeg()
        fnval = 4048 if ffmpeg_path else 1

        # 取 wbi 签名 key（nav 同时会告诉我们 cookie 是否被 B 站认作已登录）
        wbi = _BilibiliWbi()
        try:
            nav = await client.get("https://api.bilibili.com/x/web-interface/nav")
            nav_data = (nav.json() or {}).get("data") or {}
            wbi_data = nav_data.get("wbi_img") or {}
            if wbi_data.get("img_url") and wbi_data.get("sub_url"):
                wbi.refresh(str(wbi_data["img_url"]), str(wbi_data["sub_url"]))
                plugin.logger.info(
                    "B站 wbi 就绪（登录态：%s）"
                    % (
                        "已登录，可用 1080p+"
                        if nav_data.get("isLogin")
                        else "未登录，最高 480p DASH / 720p mp4"
                    )
                )
        except Exception as error:  # noqa: BLE001 - 拿不到 wbi key 就退回未签名接口
            plugin.logger.warning(f"B站 wbi key 获取失败：{error}")

        play_params: Dict[str, Any] = {
            "bvid": data.get("bvid") or ids["bvid"],
            "cid": cid,
            "qn": 120,
            "fnval": fnval,
            "fnver": 0,
            "fourk": 1,
        }
        if wbi.mixin_key:
            play = await client.get(
                "https://api.bilibili.com/x/player/wbi/playurl", params=wbi.sign(play_params)
            )
        else:
            play = await client.get(
                "https://api.bilibili.com/x/player/playurl", params=play_params
            )
        play.raise_for_status()
        play_payload = play.json()
        if play_payload.get("code") != 0:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": (
                    f"B站播放接口返回 {play_payload.get('code')}：{play_payload.get('message')}"
                    "（部分视频需要登录或仅限特定地区）"
                ),
            }

        play_data = play_payload.get("data") or {}

        # ---- DASH 分支：视频流 + 音频流分开下，再用 ffmpeg 无损合并 ----
        # 注意：B 站匿名用户的 DASH 只给到 480p，而单文件 mp4 能到 720p。
        # 所以只有 DASH 达到 1080p（需要登录 Cookie）时才值得走合并，否则退回 mp4。
        if fnval != 1:
            dash = play_data.get("dash") or {}
            video_stream, audio_stream = _pick_dash_streams(dash.get("video"), dash.get("audio"))
            if video_stream and audio_stream:
                try:
                    quality = int(video_stream.get("id") or 0)
                except (TypeError, ValueError):
                    quality = 0
                label = _BILI_QUALITY_LABELS.get(quality, f"quality-{quality}")
                if quality >= 80:
                    total_pages = len(pages)
                    title = str(data.get("title") or "").strip()
                    if total_pages > 1:
                        title = f"{title}（P{page_index + 1}/{total_pages}）"
                    plugin.logger.info(
                        f"B站解析：{title[:40]} 清晰度={label}（DASH，待 ffmpeg 合并）"
                    )
                    return {
                        "ok": True,
                        "platform": "bilibili",
                        "mode": "dash",
                        "title": title,
                        "aweme_id": data.get("bvid") or ids["bvid"] or ids["aid"],
                        "duration_ms": int(
                            page_info.get("duration") or data.get("duration") or 0
                        )
                        * 1000,
                        "cover": data.get("pic"),
                        "dash": {
                            "video_url": video_stream.get("baseUrl"),
                            "audio_url": audio_stream.get("baseUrl"),
                        },
                        "videos": [
                            {
                                "label": label,
                                "url": video_stream.get("baseUrl"),
                                "size": None,
                                "quality": quality,
                            }
                        ],
                    }
                plugin.logger.info(f"B站 DASH 最高只有 {label}，改走单文件 mp4（匿名可到 720p）")
            else:
                plugin.logger.warning("B站没有返回 DASH 流，改走单文件 mp4")
            fallback = await client.get(
                "https://api.bilibili.com/x/player/playurl",
                params={
                    "bvid": data.get("bvid") or ids["bvid"],
                    "cid": cid,
                    "qn": 64,
                    "fnval": 1,
                    "fourk": 1,
                },
            )
            if fallback.status_code == 200:
                fallback_payload = fallback.json()
                if fallback_payload.get("code") == 0:
                    play_data = fallback_payload.get("data") or play_data

        segments = play_data.get("durl") or []
        if not segments:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": "B站没有返回单文件 mp4 直链（这类视频通常需要登录才能取到 DASH 流）。",
            }
        if len(segments) > 1:
            return {
                "ok": False,
                "platform": "bilibili",
                "error": f"这条视频被切成了 {len(segments)} 段，需要 ffmpeg 合并，暂不支持。",
            }

        segment = segments[0]
        quality = play_data.get("quality")
        try:
            label = _BILI_QUALITY_LABELS.get(int(quality), f"quality-{quality}")
        except (TypeError, ValueError):
            label = "未知清晰度"

        total_pages = len(pages)
        title = str(data.get("title") or "").strip()
        if total_pages > 1:
            title = f"{title}（P{page_index + 1}/{total_pages}）"

        plugin.logger.info(
            f"B站解析：{title[:40]} 清晰度={label} 体积={_human_size(segment.get('size'))}"
        )
        return {
            "ok": True,
            "platform": "bilibili",
            "mode": "direct_url",
            "title": title,
            "aweme_id": data.get("bvid") or ids["bvid"] or ids["aid"],
            "duration_ms": int(page_info.get("duration") or data.get("duration") or 0) * 1000,
            "cover": data.get("pic"),
            "videos": [
                {
                    "label": label,
                    "url": segment.get("url"),
                    "size": segment.get("size"),
                    "quality": quality,
                }
            ],
        }


async def _obtain_video(url: str, task_dir: Path) -> "_MediaSource":
    """拿到可发送的视频。

    - 抖音 / 小红书：本地解析器取直链 → 服务器流式下载 → 走临时链接
    - TikTok：大陆直连被墙、云端完全不可达，所以由**本机**解析器走代理下好文件，
      这里只拿到一个本机路径，直接交给同机的 NapCat 上传（不经过服务器）
    - 其它平台（X 等）：yt-dlp 下载

    只有在**解析**这一步不可用（解析器离线 / 超时 / 解析失败）时，小红书才回退 yt-dlp；
    直链下载失败会直接报错，不去做无意义的回退。抖音、TikTok 没有回退路径。
    """
    config = plugin.get_config()
    max_bytes = int(config.max_file_size_mb) * 1024 * 1024
    platform = _platform_of(url)

    # B 站：走公开 API；有 ffmpeg 时用 DASH 拿 1080p/4K 并合并，否则退回单文件 mp4（匿名 720p）
    if _is_bilibili(url):
        bili_limit = int(getattr(config, "bilibili_max_size_mb", 300) or 300) * 1024 * 1024
        resolved_bili = await _resolve_bilibili(url)
        if not resolved_bili.get("ok"):
            raise RuntimeError(str(resolved_bili.get("error") or "B站解析失败"))
        if resolved_bili.get("mode") == "dash":
            try:
                dash_path, dash_name = await _download_bilibili_dash(
                    resolved_bili, task_dir, bili_limit
                )
                return _MediaSource(
                    name=dash_name, path=dash_path, size=dash_path.stat().st_size
                )
            except Exception as error:  # noqa: BLE001 - DASH 失败就退回 720p mp4
                plugin.logger.warning(f"B站 DASH 合并失败，回退单文件 mp4：{error}")
                resolved_bili = await _resolve_bilibili(url, force_mp4=True)
                if not resolved_bili.get("ok"):
                    raise RuntimeError(
                        str(resolved_bili.get("error") or "B站解析失败")
                    ) from error
        picked = _pick_stream(resolved_bili.get("videos") or [], bili_limit)
        if not picked:
            raise RuntimeError("B站没有返回可用的视频直链。")
        if picked.get("size") and int(picked["size"]) > bili_limit:
            raise RuntimeError(
                f"B站视频 {_human_size(picked['size'])} 超过 {_human_size(bili_limit)} 上限，未发送。"
                "可在插件配置里调大「B站视频体积上限」。"
            )
        return await _download_resolved(resolved_bili, "bilibili", task_dir, bili_limit)

    if platform and bool(config.resolver_enabled):
        resolved: Optional[Dict[str, Any]] = None
        if not _resolver_token():
            if platform in ("douyin", "tiktok"):
                raise RuntimeError(f"{_source_name(url)}解析需要配置「本地解析器通信密钥」，请在插件配置里填写。")
            plugin.logger.warning("未配置解析器密钥，小红书回退 yt-dlp")
        elif not _resolver_online():
            if platform in ("douyin", "tiktok"):
                raise RuntimeError(
                    "本地解析器未连接：请在 Windows 电脑上运行 media-resolver（见插件 README）。"
                    f"{_source_name(url)}无法用其它方式解析。"
                )
            plugin.logger.warning("本地解析器未连接，小红书回退 yt-dlp")
        else:
            try:
                resolved = await _resolve_via_local(url)
            except Exception as error:  # noqa: BLE001 - 超时等情况
                if platform in ("douyin", "tiktok"):
                    raise
                plugin.logger.warning(f"本地解析器超时，小红书回退 yt-dlp：{error}")

        if resolved is not None:
            if not resolved.get("ok"):
                message = str(resolved.get("error") or "本地解析器解析失败")
                if platform in ("douyin", "tiktok"):
                    raise RuntimeError(message)
                plugin.logger.warning(f"小红书解析失败，回退 yt-dlp：{message}")
            elif (resolved.get("image_list") or []) and not (resolved.get("videos") or []):
                # 图文笔记：解析器返回的是图片直链列表，没有视频流
                images = [str(item) for item in (resolved.get("image_list") or []) if item]
                limit = max(1, int(getattr(config, "max_images", 9) or 9))
                picked_images = images[:limit]
                plugin.logger.info(
                    f"{_source_name(url)}图文笔记：共 {len(images)} 张，本次发送 {len(picked_images)} 张"
                )
                if not picked_images:
                    raise RuntimeError(f"{_source_name(url)}图文笔记没有可用的图片直链")
                return _MediaSource(
                    name=(
                        f"{_PLATFORM_NAMES.get(platform or '', platform or 'media')}-"
                        f"{resolved.get('note_id') or resolved.get('aweme_id') or int(time.time())}"
                    ),
                    images=tuple(picked_images),
                )
            elif resolved.get("mode") == "local_file" and resolved.get("local_path"):
                # 解析器已在它那一侧下好文件（TikTok）。若它顺手把文件上传到了服务器
                # （NapCat 上云后必需），就用服务器侧文件走临时链接交付；
                # 否则退回旧行为：把本机路径交给同机的 NapCat。
                uploaded = _take_upload(str(resolved.get("id") or ""))
                if uploaded is not None:
                    size = uploaded.stat().st_size
                    plugin.logger.info(
                        f"使用解析器上传的文件：{uploaded.name} {_human_size(size)}"
                        f"（{(resolved.get('title') or '')[:40]}）"
                    )
                    return _MediaSource(name=uploaded.name, path=uploaded, size=size)
                local_path = str(resolved.get("local_path")).strip()
                if not _looks_like_local_path(local_path):
                    raise RuntimeError(f"本地解析器返回的文件路径不可用：{local_path[:120]}")
                allow_local = bool(getattr(config, "allow_resolver_local_path", False))
                if re.match(r"^[A-Za-z]:[\\/]", local_path) and os.name != "nt" and not allow_local:
                    # 服务器不是 Windows，却收到 Windows 本机路径：说明 NapCat 已不在解析器同一台机器上，
                    # 而解析器又没有上传文件（旧版行为）。直接给出可操作的错误，别把路径丢给 NapCat 让它失败。
                    # 若 NapCat 其实与解析器同机（家里跑 QQ/NapCat 的情况），打开配置项
                    # allow_resolver_local_path 即可按本机路径交付。
                    raise RuntimeError(
                        "TikTok 需要解析器把文件上传到服务器（NapCat 已不在解析器同一台机器上），"
                        "但本次没有收到上传。请把 media-resolver 更新到带上传能力的版本并重启它；"
                        "若 NapCat 与解析器其实是同一台机器，请在插件配置里打开「允许直接用解析器的本机路径」。"
                        f"当前收到的是本机路径：{local_path[:80]}"
                    )
                name = Path(local_path).name or "video.mp4"
                plugin.logger.info(
                    f"使用本机文件：{local_path}（{(resolved.get('title') or '')[:40]} "
                    f"{_human_size(resolved.get('size'))}）"
                )
                return _MediaSource(name=name, local_path=local_path, size=int(resolved.get("size") or 0))
            else:
                return await _download_resolved(resolved, platform, task_dir, max_bytes)

    video, name = await _run_ytdlp(url, task_dir)
    return _MediaSource(name=name, path=video, size=video.stat().st_size)


async def _download_resolved(
    resolved: Dict[str, Any], platform: str, task_dir: Path, max_bytes: int
) -> "_MediaSource":
    """按体积上限挑一条直链并下载（这里失败就直接报错，不回退）。"""
    picked = _pick_stream(resolved.get("videos") or [], max_bytes)
    if not picked:
        raise RuntimeError("本地解析器没有返回可用的视频直链")
    if picked.get("size") and int(picked["size"]) > max_bytes:
        raise RuntimeError(
            f"该视频最小体积 {_human_size(picked['size'])} 仍超过 {_human_size(max_bytes)} 上限，未发送。"
        )

    headers = {
        "User-Agent": _DOWNLOAD_UA,
        "Referer": _DOWNLOAD_REFERER.get(platform, "https://www.douyin.com/"),
        "Accept": "*/*",
    }
    source_name = _PLATFORM_NAMES.get(platform, platform)
    plugin.logger.info(
        f"{source_name}直链下载：{(resolved.get('title') or '')[:40]} 清晰度={picked.get('label')} "
        f"体积={_human_size(picked.get('size'))}"
    )
    try:
        video = await _download_direct(str(picked["url"]), task_dir / "video.mp4", max_bytes, headers)
    except Exception as error:  # noqa: BLE001 - 直链过期 / CDN 拒绝时给出可读提示
        raise RuntimeError(f"{source_name}直链下载失败：{error}") from error
    label = str(picked.get("label") or platform)
    media_id = resolved.get("aweme_id") or resolved.get("note_id") or int(time.time())
    return _MediaSource(name=f"{label}-{media_id}.mp4", path=video, size=video.stat().st_size)


# ---------------------------------------------------------------- 发送


def _lease_url(video: Path, name: str) -> str:
    _cleanup_expired()
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + int(plugin.get_config().expiry_minutes) * 60
    _LEASES[token] = _Lease(path=video, expires_at=expires_at, download_name=name)
    base = plugin.get_config().public_base_url.rstrip("/")
    return f"{base}/plugins/local_video.video_forward/media/{token}"


def _target(ctx: AgentCtx) -> tuple[str, int]:
    match = re.search(r"onebot_v11-(group|private)_(\d+)$", str(ctx.chat_key))
    if not match:
        raise RuntimeError("当前聊天不是可识别的 QQ 私聊或群聊。")
    return match.group(1), int(match.group(2))


async def _send_forward(
    ctx: AgentCtx,
    file_ref: Optional[str],
    name: str,
    source_url: str,
    images: Optional[list] = None,
) -> None:
    """把视频或图文以合并转发发出。

    ``file_ref`` 既可以是服务器上的临时链接（http URL），
    也可以是 NapCat 所在机器上的本地文件路径（TikTok 用这种）。
    ``images`` 非空时改为发图片组（图文笔记），每个节点放一张图。
    """
    kind, target = _target(ctx)
    source = _source_name(source_url)
    if images:
        content: list = [{
            "type": "text",
            "data": {"text": f"{source} 图文（{len(images)} 张）\n链接：{source_url}"},
        }]
        for reference in images:
            content.append({"type": "image", "data": {"file": reference}})
    else:
        content = [
            {"type": "text", "data": {"text": f"{source} 视频\n链接：{source_url}"}},
            {"type": "video", "data": {"file": file_ref, "name": name}},
        ]
    bot = get_bot()
    messages = [{
        "type": "node",
        "data": {
            # 合并转发节点必须挂在机器人自己的账号下，这里取 bot.self_id 而不是硬编码
            "user_id": str(getattr(bot, "self_id", "") or ""),
            "nickname": str(plugin.get_config().forward_nickname or "机器人"),
            "content": content,
        },
    }]
    # NapCat 需要先把视频上传到 QQ 服务器，几十 MB 的视频往往要一两分钟；
    # nonebot 的 OneBot 适配器默认 api_timeout 只有 30 秒，这里按调用放宽。
    # （适配器实现：timeout = data.get("_timeout", self.config.api_timeout)）
    call_timeout = int(plugin.get_config().send_timeout)
    if kind == "group":
        await bot.call_api(
            "send_group_forward_msg", group_id=target, messages=messages, _timeout=call_timeout
        )
    else:
        await bot.call_api(
            "send_private_forward_msg", user_id=target, messages=messages, _timeout=call_timeout
        )


async def _process(ctx: AgentCtx, url: str) -> str:
    if ctx.adapter_key != "onebot_v11":
        return "这个功能目前只支持 QQ / OneBot。"
    key = (str(ctx.chat_key), url)
    now = time.time()
    if key in _IN_FLIGHT:
        return "这条链接正在处理中，请稍候。"
    if _RECENT_URLS.get(key, 0) > now:
        return "这条链接刚处理过，未重复发送。"
    last = _LAST_CHAT_REQUEST.get(str(ctx.chat_key), 0)
    if now - last < int(plugin.get_config().cooldown_seconds):
        return "请稍等几秒再发下一条视频链接。"

    _IN_FLIGHT.add(key)
    _LAST_CHAT_REQUEST[str(ctx.chat_key)] = now
    _VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    task_dir = _VIDEO_DIR / secrets.token_urlsafe(12)
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        await ctx.send_text(f"正在解析{_source_name(url)}内容，请稍候…", record=False)
        source = await _obtain_video(url, task_dir)
        if source.images:
            # 图文笔记：逐张下载到服务器，再用临时链接合成一条合并转发
            referer = _DOWNLOAD_REFERER.get(
                _platform_of(url) or "xiaohongshu", "https://www.xiaohongshu.com/"
            )
            headers = {"User-Agent": _DOWNLOAD_UA, "Referer": referer, "Accept": "*/*"}
            max_bytes = int(plugin.get_config().max_file_size_mb) * 1024 * 1024
            refs: list = []
            for index, image_url in enumerate(source.images, start=1):
                image_path = await _download_direct(
                    image_url, task_dir / f"image-{index:02d}.jpg", max_bytes, headers
                )
                refs.append(_lease_url(image_path, f"{source.name}-{index:02d}.jpg"))
            plugin.logger.info(f"{_source_name(url)}图文发送：{len(refs)} 张（{source.name}）")
            await _send_forward(ctx, None, source.name, url, images=refs)
            _RECENT_URLS[key] = time.time() + 30 * 60
            return f"图文已通过合并转发发回原聊天（{len(refs)} 张图片）。"
        if source.local_path:
            # 文件就在 NapCat 那台机器上（TikTok），直接把路径交给它上传，服务器不中转
            file_ref = source.local_path
            # 这条路径没用到服务器临时目录，顺手回收
            shutil.rmtree(task_dir, ignore_errors=True)
        else:
            if source.path is None:
                raise RuntimeError("没有拿到可发送的视频文件")
            file_ref = _lease_url(source.path, source.name)
        await _send_forward(ctx, file_ref, source.name, url)
        _RECENT_URLS[key] = time.time() + 30 * 60
        return "视频已通过合并转发发回原聊天。"
    except Exception as error:
        # 失败时立刻回收临时目录；成功时文件由 lease 指向，交给过期清理。
        shutil.rmtree(task_dir, ignore_errors=True)
        plugin.logger.exception("短视频转发失败")
        return f"视频未能发送：{error}"
    finally:
        _IN_FLIGHT.discard(key)


@plugin.mount_on_user_message()
async def auto_forward_video(ctx: AgentCtx, message: ChatMessage) -> MsgSignal | None:
    """收到受支持站点的公开链接时自动处理，不再额外调用 AI。"""
    url = _find_message_url(message)
    if not url:
        return None
    result = await _process(ctx, url)
    if result.startswith("视频未能") or result.startswith("请稍等") or result.startswith("这条链接"):
        await ctx.send_text(result, record=False)
    return MsgSignal.BLOCK_TRIGGER


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="解析并合并转发短视频",
    description="下载公开的抖音、小红书、TikTok 视频链接，并以 QQ 合并转发发送到当前聊天。不支持 B 站、付费、私密或受保护视频。",
)
async def forward_video(ctx: AgentCtx, url: str) -> str:
    """当用户明确要求解析短视频时调用；参数必须是公开的抖音、小红书、TikTok 或 B站 链接。"""
    candidate = _find_supported_url(url)
    if not candidate:
        return "只支持公开的抖音、小红书、TikTok 和 B站 视频链接。"
    return await _process(ctx, candidate)


# ---------------------------------------------------------------- 路由


class ResolverResult(BaseModel):
    """本地解析器回传的结果。

    注意：这里必须显式声明或用 ``extra="allow"``，否则 Pydantic 会静默丢掉
    解析器新增的字段（例如 TikTok 的 ``mode`` / ``local_path``）。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    ok: bool = False
    error: Optional[str] = None
    platform: Optional[str] = None
    # direct_url：解析器只回传直链，由服务器下载
    # local_file：解析器已经在本机把文件下好了（TikTok），服务器直接用这个路径
    mode: Optional[str] = None
    local_path: Optional[str] = None
    size: Optional[int] = None
    aweme_id: Optional[str] = None
    note_id: Optional[str] = None
    media_id: Optional[str] = None
    title: Optional[str] = None
    cover: Optional[str] = None
    duration_ms: Optional[int] = None
    elapsed_ms: Optional[int] = None
    videos: List[Dict[str, Any]] = Field(default_factory=list)


@plugin.mount_router()
def create_router() -> APIRouter:
    """Expose media leases for NapCat plus the local resolver job API."""
    router = APIRouter()

    @router.get("/")
    async def status():
        """Small deployment check; it exposes no configuration or media."""
        return {
            "name": "video_forward",
            "version": "0.9.0",
            "supported": ["douyin", "xiaohongshu", "tiktok", "bilibili"],
            "bilibili": {
                "cookie": bool(
                    str(getattr(plugin.get_config(), "bilibili_cookie", "") or "").strip()
                ),
                "ffmpeg": bool(_bilibili_ffmpeg()),
                "quality_hint": (
                    "cookie 已配置：DASH 可达 1080p+/4K（走 ffmpeg 合并）"
                    if str(getattr(plugin.get_config(), "bilibili_cookie", "") or "").strip()
                    else "cookie 为空：匿名只能 720p（单文件 mp4）"
                ),
            },
            "resolver": {
                "enabled": bool(plugin.get_config().resolver_enabled),
                "configured": bool(_resolver_token()),
                "online": _resolver_online(),
                "last_poll_seconds_ago": round(time.time() - _LAST_POLL, 1) if _LAST_POLL else None,
                "pending_jobs": len(_JOBS),
                "stats": dict(_STATS),
            },
        }

    @router.get("/resolver/poll")
    async def resolver_poll(token: str = "", wait: int = 20):
        """本地解析器轮询取任务（长轮询，最多等 wait 秒）。"""
        global _LAST_POLL
        if not _resolver_authorized(token):
            _STATS["poll_denied"] = _STATS.get("poll_denied", 0) + 1
            raise HTTPException(status_code=403, detail="解析器密钥不正确")
        _LAST_POLL = time.time()
        _STATS["poll"] += 1
        deadline = time.time() + max(0, min(int(wait or 0), 25))
        while True:
            job = _next_pending_job()
            if job is not None:
                return {"job": {"id": job.id, "url": job.url, "created_at": job.created_at}}
            remaining = deadline - time.time()
            if remaining <= 0:
                return {"job": None}
            waiter: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
            _WAITERS.add(waiter)
            try:
                await asyncio.wait_for(waiter, timeout=remaining)
            except asyncio.TimeoutError:
                return {"job": None}
            finally:
                _WAITERS.discard(waiter)

    @router.post("/resolver/result")
    async def resolver_result(payload: ResolverResult, token: str = ""):
        """本地解析器回传解析结果。"""
        global _LAST_POLL
        if not _resolver_authorized(token):
            raise HTTPException(status_code=403, detail="解析器密钥不正确")
        _LAST_POLL = time.time()
        job = _JOBS.get(payload.id)
        if job is None:
            return {"ok": False, "detail": "任务不存在或已超时"}
        if not job.future.done():
            job.future.set_result(payload.model_dump())
        _STATS["ok" if payload.ok else "failed"] += 1
        return {"ok": True}

    @router.get("/resolver/self-test")
    async def resolver_self_test(token: str = "", url: str = "", download: bool = False):
        """自检：让本地解析器解析一个抖音 / 小红书链接（可选顺带下载验证 CDN 是否可用）。

        浏览器直接打开也能用，确认本地解析器是否真的工作：
        ``http://<服务器>:8022/plugins/local_video.video_forward/resolver/self-test?token=<密钥>&url=<链接>&download=true``
        """
        if not _resolver_authorized(token):
            raise HTTPException(status_code=403, detail="解析器密钥不正确")
        target_url = (url or "").strip() or "https://www.douyin.com/video/7684314365076960202"
        started = time.time()
        if _is_bilibili(target_url):
            # B 站不需要本地解析器：走公开 API 拿单文件 mp4 直链
            try:
                resolved = await _resolve_bilibili(target_url)
            except Exception as error:  # noqa: BLE001 - 自检需要看到原始错误
                return {"ok": False, "error": str(error)}
        else:
            if not _needs_local_resolver(target_url):
                return {"ok": False, "error": "自检只支持 抖音 / 小红书 / TikTok / B站 链接"}
            if not _resolver_online():
                return {"ok": False, "error": "本地解析器未连接（没有收到心跳）"}
            try:
                resolved = await _resolve_via_local(target_url)
            except Exception as error:  # noqa: BLE001 - 自检需要看到原始错误
                return {"ok": False, "error": str(error)}
        report: Dict[str, Any] = {
            "ok": bool(resolved.get("ok")),
            "platform": resolved.get("platform") or _platform_of(target_url),
            "mode": resolved.get("mode") or "direct_url",
            "elapsed_seconds": round(time.time() - started, 1),
            "title": resolved.get("title"),
            "media_id": resolved.get("aweme_id") or resolved.get("note_id") or resolved.get("media_id"),
            "duration_seconds": round((resolved.get("duration_ms") or 0) / 1000, 1) or None,
            "video_count": len(resolved.get("videos") or []),
            "image_count": len(resolved.get("image_list") or []),
            "error": resolved.get("error"),
        }
        if resolved.get("mode") == "local_file":
            # 解析器已经在本机下好了文件，服务器不需要再下载
            report["local_path"] = resolved.get("local_path")
            report["local_size_mb"] = round((resolved.get("size") or 0) / 1048576, 2) or None
            job_id = str(resolved.get("id") or "")
            report["upload_received"] = job_id in _UPLOADS
            report["local_path_mode"] = (
                "hand-to-napcat"
                if bool(getattr(plugin.get_config(), "allow_resolver_local_path", False))
                else "needs-upload"
            )
            if not report["upload_received"]:
                if report["local_path_mode"] == "hand-to-napcat":
                    report["hint"] = (
                        "解析器没有上传文件，但已开启「允许直接用解析器的本机路径」，"
                        "因此该路径会直接交给同机的 NapCat（无需上传）。"
                    )
                else:
                    report["hint"] = (
                        "解析器没有上传文件（旧版行为）。NapCat 已不在解析器同一台机器时，"
                        "TikTok 需要解析器先上传：请把 media-resolver 更新到带上传能力的版本并重启它；"
                        "若两者其实是同一台机器，请在插件配置里打开「允许直接用解析器的本机路径」。"
                    )
            return report
        if download and resolved.get("ok") and (resolved.get("image_list") or []):
            # 图文笔记：只验证第一张能否下载，足以证明图片链路可用
            max_bytes = int(plugin.get_config().max_file_size_mb) * 1024 * 1024
            task_dir = _VIDEO_DIR / f"selftest-img-{int(time.time())}"
            task_dir.mkdir(parents=True, exist_ok=True)
            try:
                image = await _download_direct(
                    str((resolved.get("image_list") or [])[0]),
                    task_dir / "image-01.jpg",
                    max_bytes,
                    {
                        "User-Agent": _DOWNLOAD_UA,
                        "Referer": _DOWNLOAD_REFERER.get(
                            _platform_of(target_url) or "xiaohongshu", "https://www.xiaohongshu.com/"
                        ),
                    },
                )
                report["first_image_mb"] = round(image.stat().st_size / 1048576, 2)
            except Exception as error:  # noqa: BLE001 - 自检要把错误原样报出来
                report["ok"] = False
                report["error"] = f"图片下载失败：{error}"
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)
            return report
        if download and resolved.get("ok") and resolved.get("mode") == "dash":
            # B 站 DASH：真下载两路流并用 ffmpeg 合并，才能证明 1080p 链路可用
            dash_limit = (
                int(getattr(plugin.get_config(), "bilibili_max_size_mb", 300) or 300)
                * 1024
                * 1024
            )
            task_dir = _VIDEO_DIR / f"selftest-dash-{int(time.time())}"
            task_dir.mkdir(parents=True, exist_ok=True)
            try:
                merged_path, _merged_name = await _download_bilibili_dash(
                    resolved, task_dir, dash_limit
                )
                report["downloaded_mb"] = round(merged_path.stat().st_size / 1048576, 2)
                report["merged"] = True
            except Exception as error:  # noqa: BLE001 - 自检要把错误原样报出来
                report["ok"] = False
                report["error"] = f"DASH 合并失败：{error}"
            finally:
                shutil.rmtree(task_dir, ignore_errors=True)
            return report
        if download and resolved.get("ok"):
            platform_key = str(resolved.get("platform") or _platform_of(target_url) or "douyin")
            max_bytes = int(plugin.get_config().max_file_size_mb) * 1024 * 1024
            if platform_key == "bilibili":
                max_bytes = (
                    int(getattr(plugin.get_config(), "bilibili_max_size_mb", 300) or 300)
                    * 1024
                    * 1024
                )
            picked = _pick_stream(resolved.get("videos") or [], max_bytes)
            if picked:
                task_dir = _VIDEO_DIR / f"selftest-{int(time.time())}"
                task_dir.mkdir(parents=True, exist_ok=True)
                try:
                    video = await _download_direct(
                        str(picked["url"]),
                        task_dir / "video.mp4",
                        max_bytes,
                        {
                            "User-Agent": _DOWNLOAD_UA,
                            "Referer": _DOWNLOAD_REFERER.get(
                                platform_key, "https://www.douyin.com/"
                            ),
                        },
                    )
                    report["downloaded_mb"] = round(video.stat().st_size / 1048576, 2)
                except Exception as error:  # noqa: BLE001 - 自检要把错误原样报出来
                    report["ok"] = False
                    report["error"] = f"直链下载失败：{error}"
                finally:
                    shutil.rmtree(task_dir, ignore_errors=True)
        return report

    @router.post("/resolver/upload")
    async def resolver_upload(request: Request, job: str = "", name: str = "", token: str = ""):
        """本地解析器把已下载好的文件推给服务器。

        QQ/NapCat 上云后不再与解析器同机，``local_path`` 无法使用，
        因此解析器在回报 ``mode=local_file`` 之前先调用本接口上传文件；
        插件随后用 ``/media/{token}`` 临时链接交付给 NapCat。
        """
        global _LAST_POLL
        if not _resolver_authorized(token):
            raise HTTPException(status_code=403, detail="解析器密钥不正确")
        _LAST_POLL = time.time()
        job_id = (job or "").strip()
        if not job_id:
            raise HTTPException(status_code=400, detail="缺少 job 参数")

        config = plugin.get_config()
        max_bytes = int(config.max_file_size_mb) * 1024 * 1024
        safe_name = Path(name or "video.mp4").name or "video.mp4"
        task_dir = _VIDEO_DIR / f"upload-{secrets.token_urlsafe(8)}"
        task_dir.mkdir(parents=True, exist_ok=True)
        target = task_dir / safe_name
        written = 0
        try:
            with target.open("wb") as handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"文件超过 {config.max_file_size_mb}MB 上限，已中止接收",
                        )
                    handle.write(chunk)
        except HTTPException:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise
        except Exception as error:  # noqa: BLE001 - 网络中断等
            shutil.rmtree(task_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"接收上传失败：{error}") from error

        _cleanup_expired()
        _UPLOADS[job_id] = (target, time.time() + _UPLOAD_TTL_SECONDS)
        plugin.logger.info(f"解析器上传完成（job={job_id}）：{safe_name} {_human_size(written)}")
        return {"ok": True, "size": written, "name": safe_name}

    @router.get("/media/{token}")
    async def get_media(token: str):
        _cleanup_expired()
        lease = _LEASES.get(token)
        if lease is None or not lease.path.is_file():
            raise HTTPException(status_code=404, detail="视频已过期或不存在")
        media_type = mimetypes.guess_type(lease.download_name)[0] or "application/octet-stream"
        return FileResponse(
            lease.path,
            media_type=media_type,
            filename=lease.download_name,
            content_disposition_type="inline",
        )

    return router
