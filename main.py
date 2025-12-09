"""FastAPI app for JMComic downloads with live progress/log streaming.

Run locally:
  uv run uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from contextvars import ContextVar
from dataclasses import dataclass, field
import atexit
import os
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from common.util.file_util import ZipFolder, fix_windir_name
from jmcomic import JmOption, download_album
from jmcomic.jm_config import JmModuleConfig
from jmcomic.jm_entity import JmAlbumDetail
from jmcomic.jm_exception import PartialDownloadFailedException
from jmcomic.jm_option import DirRule

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_EVENTS_PER_JOB = 400  # Maximum events to keep in memory per job
MAX_EVENTS_PER_BATCH_JOB = 500  # Maximum events for batch jobs
MONITOR_INTERVAL_SECONDS = 0.8  # Interval for file monitoring
TREE_MAX_ENTRIES = 400  # Max entries in directory tree display
MAX_RETRY_ATTEMPTS = 2  # Number of retry attempts for failed album downloads

# ---------------------------------------------------------------------------
# Logging wiring
# ---------------------------------------------------------------------------
logger = logging.getLogger("void-halo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Avoid noisy automatic domain probing in restricted environments.
JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False

_current_job: ContextVar[Optional[str]] = ContextVar("current_job", default=None)

# ---------------------------------------------------------------------------
# Smart Domain Selection
# ---------------------------------------------------------------------------
# Tracks the last domain that successfully completed a request.
# This avoids repeated SSL failures on broken domains.
_working_domain: Optional[str] = None
_working_domain_lock = threading.Lock()  # Thread-safe access


def _extract_domain_from_url(url: str) -> Optional[str]:
    """Extract domain from URL like 'https://www.cdnaspa.club/album?id=123'."""
    if not url:
        return None
    try:
        # Simple extraction: https://DOMAIN/...
        if "://" in url:
            url = url.split("://", 1)[1]
        return url.split("/", 1)[0]
    except Exception:
        return None


def _create_jm_option_with_smart_domain() -> JmOption:
    """Create JmOption with smart domain selection.

    If we have a known working domain, reorder the domain list to try it first,
    avoiding repeated SSL failures on broken domains.
    """
    option = JmOption.default()
    option.client.impl = "api"
    option.download.cache = True

    # Thread-safe read of working domain
    with _working_domain_lock:
        current_working_domain = _working_domain

    # If we have a known working domain, prioritize it
    if current_working_domain:
        try:
            domain_config = option.client.domain
            if isinstance(domain_config, dict):
                current_domains = domain_config.get("api", [])
            elif isinstance(domain_config, list):
                current_domains = domain_config
            else:
                current_domains = []

            if (
                isinstance(current_domains, list)
                and current_working_domain in current_domains
            ):
                # Move working domain to front
                reordered = [current_working_domain] + [
                    d for d in current_domains if d != current_working_domain
                ]
                if isinstance(domain_config, dict):
                    option.client.domain["api"] = reordered
                else:
                    option.client.domain = reordered
                logger.info(
                    f"[smart-domain] Prioritizing known working domain: {current_working_domain}"
                )
        except Exception as e:
            logger.warning(f"[smart-domain] Failed to reorder domains: {e}")

    return option


def _update_working_domain(domain: str) -> None:
    """Record a domain that successfully completed a request (thread-safe)."""
    global _working_domain
    if not domain:
        return
    with _working_domain_lock:
        if domain != _working_domain:
            logger.info(f"[smart-domain] Recording working domain: {domain}")
            _working_domain = domain


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    id: str
    album_id: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    message: str = ""
    title: Optional[str] = None
    zip_path: Optional[Path] = None
    final_dir: Optional[Path] = None
    work_root: Path = field(default_factory=Path)
    temp_dir: Path = field(default_factory=Path)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events: List[dict] = field(default_factory=list)
    websockets: Set[WebSocket] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "album_id": self.album_id,
            "status": self.status.value,
            "progress": round(self.progress, 3),
            "message": self.message,
            "title": self.title,
            "download_ready": self.zip_path is not None and self.zip_path.exists(),
        }


class AlbumStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    PARTIAL = "partial"  # Downloaded mostly, but some images failed
    FAILED = "failed"


@dataclass
class AlbumInfo:
    album_id: str
    title: str
    status: AlbumStatus = AlbumStatus.PENDING
    progress: float = 0.0
    error_message: Optional[str] = None
    retry_count: int = 0  # Number of retry attempts made
    failed_images: int = 0  # Number of failed images (for PARTIAL status)
    total_images: int = 0  # Total images in album

    def to_dict(self) -> dict:
        return {
            "album_id": self.album_id,
            "title": self.title,
            "status": self.status.value,
            "progress": round(self.progress, 3),
            "error_message": self.error_message,
            "retry_count": self.retry_count,
            "failed_images": self.failed_images,
            "total_images": self.total_images,
        }


@dataclass
class BatchJob:
    id: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    message: str = ""
    albums: List[AlbumInfo] = field(default_factory=list)
    current_album_index: int = -1
    zip_path: Optional[Path] = None
    work_root: Path = field(default_factory=Path)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events: List[dict] = field(default_factory=list)
    websockets: Set[WebSocket] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status.value,
            "progress": round(self.progress, 3),
            "message": self.message,
            "albums": [a.to_dict() for a in self.albums],
            "current_album_index": self.current_album_index,
            "total_albums": len(self.albums),
            "completed_albums": sum(
                1 for a in self.albums if a.status == AlbumStatus.COMPLETED
            ),
            "download_ready": self.zip_path is not None and self.zip_path.exists(),
        }


class JobManager:
    def __init__(self) -> None:
        self.jobs: Dict[str, Job] = {}
        self.job_tasks: Dict[str, asyncio.Task] = {}

        user_base = os.getenv("VOID_HALO_BASEDIR")
        if user_base:
            self.temp_dir_obj = None
            self.base_dir = Path(user_base)
            self.base_dir.mkdir(parents=True, exist_ok=True)
        else:
            # True temp folder that will be deleted on interpreter exit
            self.temp_dir_obj = tempfile.TemporaryDirectory(prefix="void-halo-")
            self.base_dir = Path(self.temp_dir_obj.name)

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = asyncio.Lock()
        self.cleanup_ttl_minutes = 120  # best-effort auto clean
        atexit.register(self._cleanup_sync)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def create_job(self, album_id: str) -> Job:
        job_id = uuid.uuid4().hex
        work_root = self.base_dir / job_id
        work_root.mkdir(parents=True, exist_ok=True)
        temp_dir = work_root / uuid.uuid4().hex[:8]
        temp_dir.mkdir(parents=True, exist_ok=True)

        job = Job(
            id=job_id,
            album_id=album_id,
            work_root=work_root,
            temp_dir=temp_dir,
        )
        self.jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    async def broadcast(self, job_id: str, event: dict) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        event = {**event, "ts": datetime.now(timezone.utc).isoformat()}
        job.events.append(event)
        if len(job.events) > MAX_EVENTS_PER_JOB:
            job.events = job.events[-MAX_EVENTS_PER_JOB:]

        if not job.websockets:
            return
        dead: List[WebSocket] = []
        for ws in list(job.websockets):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            job.websockets.discard(ws)

    def push_from_thread(self, job_id: str, event: dict) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(
            asyncio.create_task, self.broadcast(job_id, event)
        )

    def register_task(self, job_id: str, task: asyncio.Task) -> None:
        self.job_tasks[job_id] = task
        task.add_done_callback(lambda _: self.job_tasks.pop(job_id, None))

    async def cancel_job(self, job_id: str, reason: str = "用户取消") -> None:
        job = self.get(job_id)
        job.status = JobStatus.CANCELLED
        job.message = reason
        await self.broadcast(
            job_id,
            {"type": "cancelled", "message": reason, "job": job.to_dict()},
        )

        task = self.job_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task

        # NOTE: Job is NOT deleted anymore - user must manually cleanup

    async def attach_ws(self, job_id: str, ws: WebSocket) -> Job:
        job = self.get(job_id)
        await ws.accept()
        job.websockets.add(ws)
        await ws.send_json(
            {"type": "snapshot", "job": job.to_dict(), "events": job.events[-80:]}
        )
        return job

    async def detach_ws(self, job_id: str, ws: WebSocket) -> None:
        job = self.jobs.get(job_id)
        if job:
            job.websockets.discard(ws)

    async def delete_job(self, job_id: str, *, remove_files: bool = True) -> None:
        job = self.jobs.pop(job_id, None)
        if job is None:
            return
        self.job_tasks.pop(job_id, None)
        for ws in list(job.websockets):
            with contextlib.suppress(Exception):
                await ws.close(code=1001, reason="Job deleted")
        if remove_files and job.work_root.exists():
            shutil.rmtree(job.work_root, ignore_errors=True)

    async def cleanup_stale(self, ttl_minutes: int = 60) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
        stale = [jid for jid, job in self.jobs.items() if job.created_at < cutoff]
        for jid in stale:
            remove_files = self.temp_dir_obj is not None
            await self.delete_job(jid, remove_files=remove_files)

        # also sweep orphan folders that are older than ttl on disk
        if self.temp_dir_obj is None:
            return
        for path in self.base_dir.iterdir():
            if not path.is_dir():
                continue
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)

    def _cleanup_sync(self) -> None:
        """Best-effort cleanup for interpreter exit/abrupt stop."""
        try:
            if self.temp_dir_obj is not None:
                # TemporaryDirectory will clean itself
                self.temp_dir_obj.cleanup()
        except Exception:
            pass


job_manager = JobManager()


class BatchJobManager:
    """Manager for batch favorites download jobs."""

    def __init__(self, base_dir: Path, disk_cleanup_enabled: bool = True) -> None:
        self.batch_jobs: Dict[str, BatchJob] = {}
        self.job_tasks: Dict[str, asyncio.Task] = {}
        self.base_dir = base_dir
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.disk_cleanup_enabled = disk_cleanup_enabled

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def create_batch_job(self) -> BatchJob:
        job_id = uuid.uuid4().hex
        work_root = self.base_dir / f"batch_{job_id}"
        work_root.mkdir(parents=True, exist_ok=True)

        batch_job = BatchJob(
            id=job_id,
            work_root=work_root,
        )
        self.batch_jobs[job_id] = batch_job
        return batch_job

    def get(self, job_id: str) -> BatchJob:
        job = self.batch_jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    async def broadcast(self, job_id: str, event: dict) -> None:
        job = self.batch_jobs.get(job_id)
        if job is None:
            return
        event = {**event, "ts": datetime.now(timezone.utc).isoformat()}
        job.events.append(event)
        if len(job.events) > MAX_EVENTS_PER_BATCH_JOB:
            job.events = job.events[-MAX_EVENTS_PER_BATCH_JOB:]

        if not job.websockets:
            return
        dead: List[WebSocket] = []
        for ws in list(job.websockets):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            job.websockets.discard(ws)

    def push_from_thread(self, job_id: str, event: dict) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(
            asyncio.create_task, self.broadcast(job_id, event)
        )

    def register_task(self, job_id: str, task: asyncio.Task) -> None:
        self.job_tasks[job_id] = task
        task.add_done_callback(lambda _: self.job_tasks.pop(job_id, None))

    async def cancel_job(self, job_id: str, reason: str = "用户取消") -> None:
        job = self.get(job_id)
        job.status = JobStatus.CANCELLED
        job.message = reason
        await self.broadcast(
            job_id,
            {"type": "cancelled", "message": reason, "job": job.to_dict()},
        )

        task = self.job_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task

        # NOTE: We do NOT delete the job here anymore.
        # The run_batch_job function handles cleanup and may keep the job
        # if there are partial results available for download.

    async def attach_ws(self, job_id: str, ws: WebSocket) -> BatchJob:
        job = self.get(job_id)
        await ws.accept()
        job.websockets.add(ws)
        await ws.send_json(
            {"type": "snapshot", "job": job.to_dict(), "events": job.events[-100:]}
        )
        return job

    async def detach_ws(self, job_id: str, ws: WebSocket) -> None:
        job = self.batch_jobs.get(job_id)
        if job:
            job.websockets.discard(ws)

    async def delete_job(self, job_id: str, *, remove_files: bool = True) -> None:
        job = self.batch_jobs.pop(job_id, None)
        if job is None:
            return
        self.job_tasks.pop(job_id, None)
        for ws in list(job.websockets):
            with contextlib.suppress(Exception):
                await ws.close(code=1001, reason="Job deleted")
        if remove_files and job.zip_path and job.zip_path.exists():
            job.zip_path.unlink(missing_ok=True)
        if remove_files and job.work_root.exists():
            shutil.rmtree(job.work_root, ignore_errors=True)

    async def cleanup_stale(self, ttl_minutes: int = 60) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
        stale = [jid for jid, job in self.batch_jobs.items() if job.created_at < cutoff]
        for jid in stale:
            remove_files = self.disk_cleanup_enabled
            await self.delete_job(jid, remove_files=remove_files)


batch_job_manager = BatchJobManager(
    job_manager.base_dir, disk_cleanup_enabled=job_manager.temp_dir_obj is not None
)


# ---------------------------------------------------------------------------
# JMComic log bridge
# ---------------------------------------------------------------------------
# Track the last API URL for domain extraction
_last_api_url: Optional[str] = None


def _jm_log_bridge(topic: str, msg: str) -> None:
    global _last_api_url
    job_id = _current_job.get()
    formatted = f"[{topic}] {msg}"
    logger.info(formatted)

    # Capture API URLs for smart domain selection
    if topic == "api" and msg.startswith("http"):
        _last_api_url = msg

    # When we see a successful album fetch, record the working domain
    if topic == "album.before" and _last_api_url:
        domain = _extract_domain_from_url(_last_api_url)
        if domain:
            _update_working_domain(domain)

    if job_id is not None:
        job_manager.push_from_thread(job_id, {"type": "log", "message": formatted})


# 将 jmcomic 的日志执行器替换为前端桥接函数
JmModuleConfig.EXECUTOR_LOG = _jm_log_bridge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _safe_name(name: str) -> str:
    return fix_windir_name(name.strip()) or "untitled"


def _select_cover_image(root: Path) -> Optional[Path]:
    """Select cover image under root.

    Priority: exact file name '00001.webp' (case-insensitive),
    otherwise the lexicographically first image file.
    """
    if not root.exists():
        return None

    images = sorted(
        (
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in IMG_SUFFIXES
        ),
        key=lambda p: p.relative_to(root).as_posix().lower(),
    )
    if not images:
        return None

    for img in images:
        if img.name.lower() == "00001.webp":
            return img
    return images[0]


def _write_cover(root: Path, dest_cover: Path) -> None:
    """Ensure cover.webp exists based on images under root."""
    dest_cover.parent.mkdir(parents=True, exist_ok=True)
    candidate = _select_cover_image(root)
    if candidate:
        dest_cover.write_bytes(candidate.read_bytes())
    else:
        dest_cover.touch()


def _zip_chapter(src_dir: Path, dest_cbz: Path) -> None:
    dest_cbz.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_cbz, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(src_dir.iterdir()):
            if file.is_file():
                zf.write(file, arcname=file.name)


def _zip_folder(src_dir: Path, dest_zip: Path) -> None:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    ZipFolder.zip_folder(str(src_dir), str(dest_zip))


def _format_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def _build_tree_lines(root: Path, max_entries: int = 400) -> List[str]:
    if not root.exists():
        return []

    lines: List[str] = ["."]

    def walk(directory: Path, prefix: str) -> None:
        nonlocal lines
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError:
            return

        for idx, entry in enumerate(entries):
            is_last = idx == len(entries) - 1
            connector = "`- " if is_last else "|- "

            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                if len(lines) >= max_entries:
                    lines[-1] = f"{prefix}... truncated ..."
                    return
                next_prefix = prefix + ("   " if is_last else "|  ")
                walk(entry, next_prefix)
                if len(lines) >= max_entries:
                    return
            else:
                try:
                    size = _format_size(entry.stat().st_size)
                except OSError:
                    size = "?"
                lines.append(f"{prefix}{connector}{entry.name} ({size})")
                if len(lines) >= max_entries:
                    lines[-1] = f"{prefix}... truncated ..."
                    return

    walk(root, "")
    return lines


async def _monitor_files(job: Job, raw_dir: Path, total_images: Optional[int]) -> None:
    previous = -1
    while job.status == JobStatus.RUNNING:
        if not raw_dir.exists():
            await asyncio.sleep(0.6)
            continue
        count = sum(
            1
            for p in raw_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in IMG_SUFFIXES
        )
        if count != previous:
            previous = count
            pct = min(count / total_images, 0.95) if total_images else 0.0
            job.progress = pct
            job_manager.push_from_thread(
                job.id,
                {
                    "type": "progress",
                    "value": pct,
                    "message": f"已下载 {count}{'' if total_images is None else f' / {total_images}'} 张",
                },
            )
        await asyncio.sleep(0.8)


# ---------------------------------------------------------------------------
# Core job runner
# ---------------------------------------------------------------------------
async def run_job(job: Job) -> None:
    job.status = JobStatus.RUNNING
    await job_manager.broadcast(
        job.id, {"type": "status", "message": "开始下载", "job": job.to_dict()}
    )

    option = _create_jm_option_with_smart_domain()
    raw_dir = job.temp_dir / "chapters"
    # Use Pindex to avoid indextitle=None crashes; display names are handled later.
    option.dir_rule = DirRule(rule="Bd / chapter_{Pindex}", base_dir=str(raw_dir))

    # Fetch album metadata first (for total pages / naming).
    try:
        album_detail: JmAlbumDetail = await asyncio.to_thread(
            lambda: option.new_jm_client().get_album_detail(job.album_id)
        )
    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        return
    except Exception as exc:  # pragma: no cover - network dependent
        job.status = JobStatus.FAILED
        job.message = f"获取漫画信息失败: {exc}"
        await job_manager.broadcast(job.id, {"type": "error", "message": job.message})
        return

    job.title = album_detail.name
    total_images = album_detail.page_count
    job_manager.push_from_thread(
        job.id,
        {"type": "status", "message": f"目标《{job.title}》, 共 {total_images} 页"},
    )

    monitor_task = asyncio.create_task(_monitor_files(job, raw_dir, total_images))

    # Actual download
    token = _current_job.set(job.id)
    partial_info: Optional[dict] = None
    album: Optional[JmAlbumDetail] = None
    try:
        album, downloader = await asyncio.to_thread(
            download_album, job.album_id, option
        )
    except PartialDownloadFailedException as exc:
        # Partial success: keep on-disk files and continue to post-process.
        error_msg = str(exc)
        failed_match = re.search(r"共(\d+)个图片下载失败", error_msg)
        failed_count = int(failed_match.group(1)) if failed_match else 0

        downloaded_images = (
            sum(
                1
                for p in raw_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in IMG_SUFFIXES
            )
            if raw_dir.exists()
            else 0
        )
        estimated_total = total_images or (downloaded_images + failed_count)
        total_count = max(estimated_total, downloaded_images + failed_count)

        partial_info = {
            "failed_count": failed_count,
            "total_count": total_count,
            "error": error_msg,
        }
        job.message = f"部分下载失败: {failed_count}/{total_count} 张未完成"
        job_manager.push_from_thread(job.id, {"type": "error", "message": job.message})
    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        monitor_task.cancel()
        # Job persists - user can see status and retry/cleanup
        return
    except Exception as exc:  # pragma: no cover - network dependent
        job.status = JobStatus.FAILED
        job.message = f"下载失败: {exc}"
        await job_manager.broadcast(job.id, {"type": "error", "message": job.message})
        monitor_task.cancel()
        # Job persists - user can see error and retry/cleanup
        return
    finally:
        _current_job.reset(token)

    monitor_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await monitor_task

    if partial_info is not None:
        try:
            await asyncio.to_thread(
                _post_process_from_disk,
                job,
                raw_dir,
                album_detail.name,
                author=album_detail.author,
                artist=", ".join(album_detail.authors)
                if album_detail.authors
                else album_detail.author,
                description=album_detail.description,
                tags=album_detail.tags,
            )
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.message = f"后处理失败(部分下载): {exc}"
            await job_manager.broadcast(
                job.id, {"type": "error", "message": job.message}
            )
            return

        job.status = JobStatus.COMPLETED
        job.progress = 1.0
        ready_msg = f"部分完成，{partial_info['failed_count']}/{partial_info['total_count']} 张失败"
        await job_manager.broadcast(
            job.id,
            {
                "type": "ready",
                "download": f"/api/jobs/{job.id}/download",
                "job": job.to_dict(),
                "message": ready_msg,
            },
        )
        job_manager.job_tasks.pop(job.id, None)
        return

    # Post-processing
    try:
        await asyncio.to_thread(_post_process, job, album, option, raw_dir)
    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        # Job persists - user can retry/cleanup
        return
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.message = f"后处理失败: {exc}"
        await job_manager.broadcast(job.id, {"type": "error", "message": job.message})
        # Job persists - user can see error and retry/cleanup
        return

    job.status = JobStatus.COMPLETED
    job.progress = 1.0
    await job_manager.broadcast(
        job.id,
        {
            "type": "ready",
            "download": f"/api/jobs/{job.id}/download",
            "job": job.to_dict(),
            "message": "下载完成，点击下载按钮打包",
        },
    )

    # NOTE: No auto-cleanup TTL anymore - user must manually cleanup
    job_manager.job_tasks.pop(job.id, None)


def _post_process(
    job: Job, album: JmAlbumDetail, option: JmOption, raw_dir: Path
) -> None:
    if len(album) == 0:
        raise ValueError("漫画无可用图片")

    safe_title = _safe_name(album.name)
    final_dir = job.work_root / safe_title

    # cover (before raw removal)
    cover_path = job.temp_dir / "cover.webp"
    _write_cover(raw_dir, cover_path)

    # details.json
    details = {
        "title": album.name,
        "author": album.author,
        "artist": ", ".join(album.authors) if album.authors else album.author,
        "description": album.description,
        "genre": album.tags,
        "status": "0",
        "_status values": [
            "0 = Unknown",
            "1 = Ongoing",
            "2 = Completed",
            "3 = Licensed",
        ],
    }
    (job.temp_dir / "details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Chapters to cbz
    for idx, photo in enumerate(album):
        chapter_dir = Path(option.dir_rule.decide_image_save_dir(album, photo))
        if not chapter_dir.exists():
            continue
        # Use indextitle if available, otherwise use index-based name
        raw_name = (
            getattr(photo, "indextitle", None)
            or getattr(photo, "name", None)
            or f"chapter_{idx + 1}"
        )
        chapter_name = _safe_name(str(raw_name))
        cbz_path = job.temp_dir / f"{chapter_name}.cbz"
        _zip_chapter(chapter_dir, cbz_path)

    # Remove raw images to keep final zip small
    if raw_dir.exists():
        shutil.rmtree(raw_dir, ignore_errors=True)

    # Rename temp dir to comic title
    job.temp_dir.rename(final_dir)
    job.temp_dir = final_dir
    job.final_dir = final_dir

    # NOTE: No longer zip here - user triggers zip on download click
    job.zip_path = None


def _post_process_from_disk(
    job: Job,
    raw_dir: Path,
    title: str,
    *,
    author: str = "Unknown",
    artist: str = "Unknown",
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> None:
    """Post-process using already-downloaded files on disk (partial success path).

    Builds cover.webp, details.json, chapter cbz files, and final folder rename
    without relying on in-memory album objects (which may be unavailable on partial failures).
    """
    if not raw_dir.exists():
        raise ValueError("缺少已下载文件，无法生成打包内容")

    safe_title = _safe_name(title)

    # cover
    cover_path = job.temp_dir / "cover.webp"
    _write_cover(raw_dir, cover_path)

    # details.json
    details = {
        "title": title,
        "author": author or "Unknown",
        "artist": artist or author or "Unknown",
        "description": description or "",
        "genre": tags or [],
        "status": "0",
    }
    (job.temp_dir / "details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # chapters on disk -> cbz
    for chapter_dir in sorted(raw_dir.iterdir()):
        if not chapter_dir.is_dir():
            continue
        chapter_name = _safe_name(chapter_dir.name)
        cbz_path = job.temp_dir / f"{chapter_name}.cbz"
        _zip_chapter(chapter_dir, cbz_path)

    # remove raw images
    shutil.rmtree(raw_dir, ignore_errors=True)

    # finalize folder
    final_dir = job.work_root / safe_title
    job.temp_dir.rename(final_dir)
    job.temp_dir = final_dir
    job.final_dir = final_dir
    job.zip_path = None


# ---------------------------------------------------------------------------
# Batch Favorites Download Runner
# ---------------------------------------------------------------------------
_current_batch_job: ContextVar[Optional[str]] = ContextVar(
    "current_batch_job", default=None
)


def _batch_jm_log_bridge(topic: str, msg: str) -> None:
    """Log bridge for batch downloads that routes to batch_job_manager."""
    global _last_api_url
    job_id = _current_batch_job.get()
    formatted = f"[{topic}] {msg}"
    logger.info(formatted)

    # Capture API URLs for smart domain selection
    if topic == "api" and msg.startswith("http"):
        _last_api_url = msg

    # When we see a successful album fetch, record the working domain
    if topic == "album.before" and _last_api_url:
        domain = _extract_domain_from_url(_last_api_url)
        if domain:
            _update_working_domain(domain)

    if job_id is not None:
        batch_job_manager.push_from_thread(
            job_id, {"type": "log", "message": formatted}
        )


async def run_batch_job(batch_job: BatchJob, username: str, password: str) -> None:
    """Run a batch favorites download job."""
    batch_job.status = JobStatus.RUNNING
    await batch_job_manager.broadcast(
        batch_job.id,
        {"type": "status", "message": "开始批量下载", "job": batch_job.to_dict()},
    )

    # Use smart domain selection for login
    option = _create_jm_option_with_smart_domain()

    # Login and fetch favorites
    try:
        client = await asyncio.to_thread(option.new_jm_client)
        await asyncio.to_thread(client.login, username, password)

        await batch_job_manager.broadcast(
            batch_job.id,
            {"type": "status", "message": "登录成功，正在获取收藏列表..."},
        )

        # Fetch all favorites
        albums: List[AlbumInfo] = []

        def fetch_favorites():
            for page in client.favorite_folder_gen():
                for aid, atitle in page.iter_id_title():
                    albums.append(AlbumInfo(album_id=str(aid), title=atitle))

        await asyncio.to_thread(fetch_favorites)

        if not albums:
            batch_job.status = JobStatus.COMPLETED
            batch_job.message = "收藏夹为空"
            await batch_job_manager.broadcast(
                batch_job.id,
                {"type": "empty", "message": "收藏夹为空", "job": batch_job.to_dict()},
            )
            return

        batch_job.albums = albums
        await batch_job_manager.broadcast(
            batch_job.id,
            {
                "type": "albums_list",
                "message": f"获取到 {len(albums)} 本收藏漫画",
                "albums": [a.to_dict() for a in albums],
                "job": batch_job.to_dict(),
            },
        )

    except asyncio.CancelledError:
        batch_job.status = JobStatus.CANCELLED
        return
    except Exception as exc:
        batch_job.status = JobStatus.FAILED
        batch_job.message = f"登录或获取收藏夹失败: {exc}"
        await batch_job_manager.broadcast(
            batch_job.id,
            {"type": "error", "message": batch_job.message},
        )
        return

    # Download each album
    token = _current_batch_job.set(batch_job.id)
    old_log_executor = JmModuleConfig.EXECUTOR_LOG
    JmModuleConfig.EXECUTOR_LOG = _batch_jm_log_bridge

    try:
        for idx, album_info in enumerate(batch_job.albums):
            if batch_job.status == JobStatus.CANCELLED:
                break

            batch_job.current_album_index = idx
            album_info.status = AlbumStatus.DOWNLOADING

            await batch_job_manager.broadcast(
                batch_job.id,
                {
                    "type": "album_start",
                    "index": idx,
                    "album": album_info.to_dict(),
                    "job": batch_job.to_dict(),
                },
            )

            try:
                partial_info = await _download_single_album_for_batch(
                    batch_job, album_info
                )

                if partial_info:
                    # Partial success - most images downloaded but some failed
                    album_info.status = AlbumStatus.PARTIAL
                    album_info.progress = 1.0
                    failed_count = partial_info.get("failed_count", 0)
                    total_count = partial_info.get("total_count", 0)
                    album_info.error_message = f"{failed_count}/{total_count} 图片失败"

                    await batch_job_manager.broadcast(
                        batch_job.id,
                        {
                            "type": "album_partial",
                            "index": idx,
                            "album": album_info.to_dict(),
                            "failed_count": failed_count,
                            "total_count": total_count,
                            "job": batch_job.to_dict(),
                        },
                    )
                else:
                    # Full success
                    album_info.status = AlbumStatus.COMPLETED
                    album_info.progress = 1.0

                    await batch_job_manager.broadcast(
                        batch_job.id,
                        {
                            "type": "album_completed",
                            "index": idx,
                            "album": album_info.to_dict(),
                            "job": batch_job.to_dict(),
                        },
                    )

            except asyncio.CancelledError:
                batch_job.status = JobStatus.CANCELLED
                break
            except Exception as exc:
                album_info.status = AlbumStatus.FAILED
                album_info.error_message = str(exc)

                await batch_job_manager.broadcast(
                    batch_job.id,
                    {
                        "type": "album_failed",
                        "index": idx,
                        "album": album_info.to_dict(),
                        "error": str(exc),
                    },
                )
                # Continue with next album

            # Update overall progress
            completed = sum(
                1
                for a in batch_job.albums
                if a.status
                in (AlbumStatus.COMPLETED, AlbumStatus.PARTIAL, AlbumStatus.FAILED)
            )
            batch_job.progress = (
                completed / len(batch_job.albums) * 0.95
            )  # Reserve 5% for final zip

    finally:
        _current_batch_job.reset(token)
        JmModuleConfig.EXECUTOR_LOG = old_log_executor

    # Auto-retry failed albums
    if batch_job.status != JobStatus.CANCELLED:
        failed_albums = [
            (idx, a)
            for idx, a in enumerate(batch_job.albums)
            if a.status == AlbumStatus.FAILED and a.retry_count < MAX_RETRY_ATTEMPTS
        ]

        if failed_albums:
            await batch_job_manager.broadcast(
                batch_job.id,
                {
                    "type": "retry_phase",
                    "count": len(failed_albums),
                    "message": f"开始重试 {len(failed_albums)} 个失败任务...",
                },
            )

            token = _current_batch_job.set(batch_job.id)
            old_log_executor = JmModuleConfig.EXECUTOR_LOG
            JmModuleConfig.EXECUTOR_LOG = _batch_jm_log_bridge

            try:
                for idx, album_info in failed_albums:
                    if batch_job.status == JobStatus.CANCELLED:
                        break

                    album_info.retry_count += 1
                    album_info.status = AlbumStatus.DOWNLOADING
                    album_info.error_message = None
                    batch_job.current_album_index = idx

                    await batch_job_manager.broadcast(
                        batch_job.id,
                        {
                            "type": "retry_start",
                            "index": idx,
                            "retry_count": album_info.retry_count,
                            "album": album_info.to_dict(),
                            "message": f"重试 ({album_info.retry_count}/{MAX_RETRY_ATTEMPTS}): {album_info.title}",
                        },
                    )

                    try:
                        await _download_single_album_for_batch(batch_job, album_info)
                        album_info.status = AlbumStatus.COMPLETED
                        album_info.progress = 1.0

                        await batch_job_manager.broadcast(
                            batch_job.id,
                            {
                                "type": "retry_completed",
                                "index": idx,
                                "album": album_info.to_dict(),
                                "message": f"重试成功: {album_info.title}",
                            },
                        )
                    except asyncio.CancelledError:
                        batch_job.status = JobStatus.CANCELLED
                        break
                    except Exception as exc:
                        album_info.status = AlbumStatus.FAILED
                        album_info.error_message = str(exc)

                        await batch_job_manager.broadcast(
                            batch_job.id,
                            {
                                "type": "retry_failed",
                                "index": idx,
                                "retry_count": album_info.retry_count,
                                "album": album_info.to_dict(),
                                "error": str(exc),
                                "message": f"重试失败 ({album_info.retry_count}/{MAX_RETRY_ATTEMPTS}): {album_info.title}",
                            },
                        )
            finally:
                _current_batch_job.reset(token)
                JmModuleConfig.EXECUTOR_LOG = old_log_executor

    if batch_job.status == JobStatus.CANCELLED:
        # Count what was completed for user info
        completed_albums = [
            a for a in batch_job.albums if a.status == AlbumStatus.COMPLETED
        ]
        await batch_job_manager.broadcast(
            batch_job.id,
            {
                "type": "cancelled",
                "completed_count": len(completed_albums),
                "total_count": len(batch_job.albums),
                "job": batch_job.to_dict(),
                "message": f"已取消 ({len(completed_albums)}/{len(batch_job.albums)} 完成)",
            },
        )
        # NOTE: Job persists - user can download partial results or cleanup
        return

    # All downloads finished - no auto-zip, user triggers on download click
    batch_job.status = JobStatus.COMPLETED
    batch_job.progress = 1.0
    batch_job.zip_path = None  # Will be created on download request

    # Count final results
    completed_albums = [
        a for a in batch_job.albums if a.status == AlbumStatus.COMPLETED
    ]
    partial_albums = [a for a in batch_job.albums if a.status == AlbumStatus.PARTIAL]
    failed_albums = [a for a in batch_job.albums if a.status == AlbumStatus.FAILED]

    # If there are still failed albums, send a summary
    if failed_albums:
        await batch_job_manager.broadcast(
            batch_job.id,
            {
                "type": "failed_summary",
                "failed_albums": [a.to_dict() for a in failed_albums],
                "completed_count": len(completed_albums),
                "partial_count": len(partial_albums),
                "failed_count": len(failed_albums),
                "total_count": len(batch_job.albums),
                "message": f"完成 {len(completed_albums) + len(partial_albums)}/{len(batch_job.albums)}，{len(failed_albums)} 本失败",
            },
        )

    # Build ready message
    success_count = len(completed_albums) + len(partial_albums)
    if failed_albums:
        ready_message = f"下载完成 ({success_count}/{len(batch_job.albums)} 成功，{len(failed_albums)} 失败)"
    elif partial_albums:
        ready_message = (
            f"下载完成 ({len(completed_albums)} 完整，{len(partial_albums)} 部分)"
        )
    else:
        ready_message = "下载完成，点击下载按钮打包"

    await batch_job_manager.broadcast(
        batch_job.id,
        {
            "type": "ready",
            "download": f"/api/batch-jobs/{batch_job.id}/download",
            "job": batch_job.to_dict(),
            "completed_count": len(completed_albums),
            "partial_count": len(partial_albums),
            "failed_count": len(failed_albums),
            "message": ready_message,
        },
    )

    batch_job_manager.job_tasks.pop(batch_job.id, None)


async def _download_single_album_for_batch(
    batch_job: BatchJob, album_info: AlbumInfo
) -> Optional[dict]:
    """Download a single album for batch job (no individual zip).

    Creates a fresh JmOption for each album with smart domain selection,
    avoiding SSL/session issues with broken domains.
    """
    album_dir = batch_job.work_root / _safe_name(album_info.title)
    album_dir.mkdir(parents=True, exist_ok=True)

    # Create fresh option with smart domain selection
    option = _create_jm_option_with_smart_domain()

    raw_dir = album_dir / "chapters"
    # Use Pindex instead of Pindextitle to avoid None errors when indextitle is not set
    option.dir_rule = DirRule(rule="Bd / chapter_{Pindex}", base_dir=str(raw_dir))

    # Progress monitor for this album (try to know total pages early)
    total_images = 0
    try:
        detail_option = _create_jm_option_with_smart_domain()

        def _fetch_page_count() -> int:
            with detail_option.new_jm_client() as client:
                detail = client.get_album_detail(album_info.album_id)
                return detail.page_count

        total_images = await asyncio.to_thread(_fetch_page_count)
        album_info.total_images = total_images
    except Exception:
        total_images = 0

    async def monitor_album():
        previous = -1
        while album_info.status == AlbumStatus.DOWNLOADING:
            if not raw_dir.exists():
                await asyncio.sleep(0.6)
                continue
            count = sum(
                1
                for p in raw_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in IMG_SUFFIXES
            )
            if count != previous:
                previous = count
                pct = min(count / total_images, 0.95) if total_images else 0.0
                album_info.progress = pct
                batch_job_manager.push_from_thread(
                    batch_job.id,
                    {
                        "type": "album_progress",
                        "index": batch_job.current_album_index,
                        "progress": pct,
                        "message": f"[{album_info.title}] 已下载 {count}/{total_images} 张",
                    },
                )
            await asyncio.sleep(0.8)

    monitor_task = asyncio.create_task(monitor_album())

    # Track partial download failures
    partial_failure_info = None

    # Use download_album's return value! The pre-fetched album_detail from
    # get_album_detail() creates JmPhotoDetail objects with page_arr=None,
    # which crashes when DirRule calls len(photo). The album returned from
    # download_album() has properly populated JmPhotoDetail objects.
    try:
        album, _downloader = await asyncio.to_thread(
            download_album, album_info.album_id, option
        )
        total_images = album.page_count
        album_info.total_images = total_images
    except PartialDownloadFailedException as exc:
        # Extract the album object - download actually completed for most files!
        # The exception message format: "部分下载失败\n\n共N个图片下载失败: [...]"
        # We need to get the album from the downloader context, but since we can't,
        # we'll parse the error to count failures and mark as partial success
        error_msg = str(exc)

        # Parse number of failed images from the message
        import re

        failed_match = re.search(r"共(\d+)个图片下载失败", error_msg)
        failed_count = int(failed_match.group(1)) if failed_match else 0

        # Re-fetch album info to get total page count
        # Use a fresh client to get album details
        try:
            fresh_option = _create_jm_option_with_smart_domain()
            with fresh_option.new_jm_client() as client:
                album_detail = client.get_album_detail(album_info.album_id)
                total_images = sum(len(p) for p in album_detail)
                album_info.total_images = total_images
        except Exception:
            # If we can't get details, estimate from downloaded files
            if raw_dir.exists():
                total_images = sum(
                    1
                    for p in raw_dir.rglob("*")
                    if p.is_file() and p.suffix.lower() in IMG_SUFFIXES
                )
                album_info.total_images = total_images + failed_count

        album_info.failed_images = failed_count
        partial_failure_info = {
            "failed_count": failed_count,
            "total_count": album_info.total_images,
            "error": error_msg,
        }

        # We still have the album reference via the error context but can't access it
        # So we'll create a minimal post-processing based on what's on disk
        album = None  # Will skip album-specific post-processing
    finally:
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task

    # Post-process: create cbz files for each chapter
    if album is not None:
        # Normal case: use album object from download_album()
        for idx, photo in enumerate(album):
            chapter_dir = Path(option.dir_rule.decide_image_save_dir(album, photo))
            if not chapter_dir.exists():
                continue
            # Use indextitle if available, otherwise use index-based name
            raw_name = (
                getattr(photo, "indextitle", None)
                or getattr(photo, "name", None)
                or f"chapter_{idx + 1}"
            )
            chapter_name = _safe_name(str(raw_name))
            cbz_path = album_dir / f"{chapter_name}.cbz"
            _zip_chapter(chapter_dir, cbz_path)

        # Write details.json with album info
        details = {
            "title": album.name,
            "author": album.author,
            "artist": ", ".join(album.authors) if album.authors else album.author,
            "description": album.description,
            "genre": album.tags,
            "status": "0",
        }
    else:
        # Partial failure case: process directories on disk
        if raw_dir.exists():
            for chapter_dir in sorted(raw_dir.iterdir()):
                if chapter_dir.is_dir():
                    chapter_name = _safe_name(chapter_dir.name)
                    cbz_path = album_dir / f"{chapter_name}.cbz"
                    _zip_chapter(chapter_dir, cbz_path)

        # Write minimal details.json
        details = {
            "title": album_info.title,
            "author": "Unknown",
            "artist": "Unknown",
            "description": f"Partial download - {album_info.failed_images} images failed",
            "genre": [],
            "status": "0",
        }

    (album_dir / "details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Ensure cover.webp exists before raw images are removed
    _write_cover(raw_dir, album_dir / "cover.webp")

    # Remove raw images folder
    if raw_dir.exists():
        shutil.rmtree(raw_dir, ignore_errors=True)

    # Return partial failure info if any
    return partial_failure_info


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle hooks for startup/shutdown using an async context manager."""

    loop = asyncio.get_running_loop()
    job_manager.set_loop(loop)
    batch_job_manager.set_loop(loop)

    await job_manager.cleanup_stale(ttl_minutes=job_manager.cleanup_ttl_minutes)
    await batch_job_manager.cleanup_stale(ttl_minutes=job_manager.cleanup_ttl_minutes)

    try:
        yield
    finally:
        await job_manager.cleanup_stale(ttl_minutes=0)
        await batch_job_manager.cleanup_stale(ttl_minutes=0)


app = FastAPI(title="Void Halo Downloader", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    index_path = Path("static/index.html")
    if not index_path.exists():
        return HTMLResponse("<h1>UI missing</h1>")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.post("/api/jobs")
async def create_job(payload: dict) -> dict:
    album_id = str(payload.get("album_id", "")).strip()
    if not album_id:
        raise HTTPException(status_code=400, detail="album_id is required")
    job = job_manager.create_job(album_id)
    task = asyncio.create_task(run_job(job))
    job_manager.register_task(job.id, task)
    return {"job_id": job.id, "status": job.status.value, "title": job.title}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str) -> dict:
    try:
        job = job_manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict()


@app.get("/api/jobs/{job_id}/tree")
async def job_tree(job_id: str) -> dict:
    try:
        job = job_manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")

    tree_lines = await asyncio.to_thread(_build_tree_lines, job.work_root)
    return {
        "job_id": job_id,
        "root": str(job.work_root),
        "tree": tree_lines,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    await job_manager.cancel_job(job_id)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    await job_manager.delete_job(job_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str) -> FileResponse:
    job = job_manager.get(job_id)
    if job.zip_path is None or not job.zip_path.exists():
        raise HTTPException(status_code=404, detail="Zip not ready")
    return FileResponse(
        job.zip_path, media_type="application/zip", filename=job.zip_path.name
    )


@app.websocket("/ws/jobs/{job_id}")
async def ws(job_id: str, websocket: WebSocket) -> None:
    try:
        await job_manager.attach_ws(job_id, websocket)
    except KeyError:
        await websocket.close(code=4404, reason="job not found")
        return

    try:
        while True:
            # Keep connection alive; we don't expect client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        await job_manager.detach_ws(job_id, websocket)
    except Exception:
        await job_manager.detach_ws(job_id, websocket)
        await websocket.close()


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# State Restoration & Lazy Packaging Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/current-job")
async def get_current_job() -> dict:
    """Get the current single download job if any exists (single user mode)."""
    if not job_manager.jobs:
        return {"job": None}
    # Return the first (and only expected) job
    job = next(iter(job_manager.jobs.values()))
    return {"job": job.to_dict()}


@app.get("/api/current-batch-job")
async def get_current_batch_job() -> dict:
    """Get the current batch download job if any exists (single user mode)."""
    if not batch_job_manager.batch_jobs:
        return {"job": None}
    # Return the first (and only expected) batch job
    batch_job = next(iter(batch_job_manager.batch_jobs.values()))
    return {
        "job": batch_job.to_dict(),
        "albums": [a.to_dict() for a in batch_job.albums],
    }


@app.post("/api/jobs/{job_id}/prepare-download")
async def prepare_job_download(job_id: str) -> dict:
    """Package job into zip for download (lazy packaging)."""
    try:
        job = job_manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")

    if job.zip_path and job.zip_path.exists():
        return {"ready": True, "download": f"/api/jobs/{job_id}/download"}

    if not job.final_dir or not job.final_dir.exists():
        raise HTTPException(status_code=400, detail="No download data available")

    # Broadcast progress
    await job_manager.broadcast(job_id, {"type": "packing", "message": "正在打包..."})

    try:
        safe_title = job.final_dir.name
        zip_path = job.work_root / f"{safe_title}.zip"
        await asyncio.to_thread(_zip_folder, job.final_dir, zip_path)
        job.zip_path = zip_path

        await job_manager.broadcast(
            job_id,
            {
                "type": "zip_ready",
                "download": f"/api/jobs/{job_id}/download",
                "message": "打包完成",
            },
        )
        return {"ready": True, "download": f"/api/jobs/{job_id}/download"}
    except Exception as exc:
        await job_manager.broadcast(
            job_id, {"type": "error", "message": f"打包失败: {exc}"}
        )
        raise HTTPException(status_code=500, detail=f"Zip failed: {exc}")


@app.post("/api/batch-jobs/{job_id}/prepare-download")
async def prepare_batch_download(job_id: str) -> dict:
    """Package batch job into zip for download (lazy packaging)."""
    try:
        batch_job = batch_job_manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="batch job not found")

    if batch_job.zip_path and batch_job.zip_path.exists():
        return {"ready": True, "download": f"/api/batch-jobs/{job_id}/download"}

    if not batch_job.work_root.exists():
        raise HTTPException(status_code=400, detail="No download data available")

    # Broadcast progress
    await batch_job_manager.broadcast(
        job_id, {"type": "packing", "message": "正在打包所有漫画..."}
    )

    try:
        zip_name = f"favorites_{job_id[:8]}.zip"
        zip_path = batch_job.work_root.parent / zip_name
        await asyncio.to_thread(_zip_folder, batch_job.work_root, zip_path)
        batch_job.zip_path = zip_path

        await batch_job_manager.broadcast(
            job_id,
            {
                "type": "zip_ready",
                "download": f"/api/batch-jobs/{job_id}/download",
                "message": "打包完成",
            },
        )
        return {"ready": True, "download": f"/api/batch-jobs/{job_id}/download"}
    except Exception as exc:
        await batch_job_manager.broadcast(
            job_id, {"type": "error", "message": f"打包失败: {exc}"}
        )
        raise HTTPException(status_code=500, detail=f"Zip failed: {exc}")


@app.post("/api/batch-jobs/{job_id}/albums/{album_id}/retry")
async def retry_album(job_id: str, album_id: str) -> dict:
    """Manually retry a failed album download."""
    try:
        batch_job = batch_job_manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="batch job not found")

    # Find the album
    album_info = None
    album_index = -1
    for idx, a in enumerate(batch_job.albums):
        if a.album_id == album_id:
            album_info = a
            album_index = idx
            break

    if album_info is None:
        raise HTTPException(status_code=404, detail="album not found in job")

    if album_info.status != AlbumStatus.FAILED:
        return {"status": "skipped", "message": "Album is not in failed state"}

    # Reset album state for retry
    album_info.retry_count += 1
    album_info.status = AlbumStatus.DOWNLOADING
    album_info.error_message = None
    batch_job.current_album_index = album_index

    await batch_job_manager.broadcast(
        job_id,
        {
            "type": "manual_retry_start",
            "index": album_index,
            "album": album_info.to_dict(),
            "message": f"手动重试: {album_info.title}",
        },
    )

    # Run the download
    try:
        token = _current_batch_job.set(job_id)
        old_log_executor = JmModuleConfig.EXECUTOR_LOG
        JmModuleConfig.EXECUTOR_LOG = _batch_jm_log_bridge

        try:
            partial_info = await _download_single_album_for_batch(batch_job, album_info)

            if partial_info:
                album_info.status = AlbumStatus.PARTIAL
                album_info.progress = 1.0
                album_info.failed_images = partial_info.get("failed_count", 0)
                album_info.total_images = partial_info.get(
                    "total_count", album_info.total_images
                )
                album_info.error_message = partial_info.get("error")

                await batch_job_manager.broadcast(
                    job_id,
                    {
                        "type": "manual_retry_partial",
                        "index": album_index,
                        "album": album_info.to_dict(),
                        "failed_count": album_info.failed_images,
                        "total_count": album_info.total_images,
                        "message": f"手动重试部分成功: {album_info.title}",
                    },
                )
            else:
                album_info.status = AlbumStatus.COMPLETED
                album_info.progress = 1.0

                await batch_job_manager.broadcast(
                    job_id,
                    {
                        "type": "manual_retry_completed",
                        "index": album_index,
                        "album": album_info.to_dict(),
                        "message": f"手动重试成功: {album_info.title}",
                    },
                )

            # Clear any existing zip since content changed
            batch_job.zip_path = None

            if partial_info:
                return {
                    "status": "partial",
                    "album": album_info.to_dict(),
                    "failed_count": album_info.failed_images,
                }
            return {"status": "success", "album": album_info.to_dict()}
        except Exception as exc:
            album_info.status = AlbumStatus.FAILED
            album_info.error_message = str(exc)

            await batch_job_manager.broadcast(
                job_id,
                {
                    "type": "manual_retry_failed",
                    "index": album_index,
                    "album": album_info.to_dict(),
                    "error": str(exc),
                    "message": f"手动重试失败: {album_info.title}",
                },
            )
            return {
                "status": "failed",
                "error": str(exc),
                "album": album_info.to_dict(),
            }
        finally:
            _current_batch_job.reset(token)
            JmModuleConfig.EXECUTOR_LOG = old_log_executor
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@app.delete("/api/cleanup")
async def cleanup_all() -> dict:
    """Delete all jobs and their temp files."""
    # Delete all single jobs
    for job_id in list(job_manager.jobs.keys()):
        await job_manager.delete_job(job_id)

    # Delete all batch jobs
    for job_id in list(batch_job_manager.batch_jobs.keys()):
        await batch_job_manager.delete_job(job_id)

    return {"ok": True, "message": "All jobs cleaned up"}


# ---------------------------------------------------------------------------
# Batch Jobs API
# ---------------------------------------------------------------------------
@app.post("/api/batch-jobs")
async def create_batch_job(payload: dict) -> dict:
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", "")).strip()
    if not username or not password:
        raise HTTPException(
            status_code=400, detail="username and password are required"
        )
    batch_job = batch_job_manager.create_batch_job()
    task = asyncio.create_task(run_batch_job(batch_job, username, password))
    batch_job_manager.register_task(batch_job.id, task)
    return {"job_id": batch_job.id, "status": batch_job.status.value}


@app.get("/api/batch-jobs/{job_id}")
async def batch_job_status(job_id: str) -> dict:
    try:
        batch_job = batch_job_manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="batch job not found")
    return batch_job.to_dict()


@app.get("/api/batch-jobs/{job_id}/tree")
async def batch_job_tree(job_id: str) -> dict:
    """Get directory tree for a batch job."""
    try:
        batch_job = batch_job_manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="batch job not found")

    tree_lines = await asyncio.to_thread(_build_tree_lines, batch_job.work_root)
    return {
        "job_id": job_id,
        "root": str(batch_job.work_root),
        "tree": tree_lines,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/batch-jobs/{job_id}/cancel")
async def cancel_batch_job(job_id: str) -> dict:
    try:
        await batch_job_manager.cancel_job(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="batch job not found")
    return {"ok": True}


@app.delete("/api/batch-jobs/{job_id}")
async def delete_batch_job(job_id: str) -> dict:
    try:
        await batch_job_manager.delete_job(job_id)
    except KeyError:
        pass  # Already deleted
    return {"ok": True}


@app.get("/api/batch-jobs/{job_id}/download")
async def batch_download(job_id: str) -> FileResponse:
    try:
        batch_job = batch_job_manager.get(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="batch job not found")
    if batch_job.zip_path is None or not batch_job.zip_path.exists():
        raise HTTPException(status_code=404, detail="Zip not ready")
    return FileResponse(
        batch_job.zip_path,
        media_type="application/zip",
        filename=batch_job.zip_path.name,
    )


@app.websocket("/ws/batch-jobs/{job_id}")
async def batch_ws(job_id: str, websocket: WebSocket) -> None:
    try:
        await batch_job_manager.attach_ws(job_id, websocket)
    except KeyError:
        await websocket.close(code=4404, reason="batch job not found")
        return

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await batch_job_manager.detach_ws(job_id, websocket)
    except Exception:
        await batch_job_manager.detach_ws(job_id, websocket)
        await websocket.close()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
