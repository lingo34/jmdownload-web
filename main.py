"""FastAPI app for JMComic downloads with live progress/log streaming.

Run locally:
  uv run uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import tempfile
import uuid
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
from jmcomic.jm_entity import JmAlbumDetail, JmPhotoDetail
from jmcomic.jm_option import DirRule

# ---------------------------------------------------------------------------
# Logging wiring
# ---------------------------------------------------------------------------
logger = logging.getLogger("void-halo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Avoid noisy automatic domain probing in restricted environments.
JmModuleConfig.FLAG_API_CLIENT_AUTO_UPDATE_DOMAIN = False

_current_job: ContextVar[Optional[str]] = ContextVar("current_job", default=None)


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
        if len(job.events) > 400:
            job.events = job.events[-400:]

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

        await self.broadcast(job_id, {"type": "cleanup", "message": "开始清理"})
        await self.delete_job(job_id)
        await self.broadcast(job_id, {"type": "cleanup_done", "message": "数据已清理"})

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

    async def delete_job(self, job_id: str) -> None:
        job = self.jobs.pop(job_id, None)
        if job is None:
            return
        self.job_tasks.pop(job_id, None)
        for ws in list(job.websockets):
            await ws.close(code=1001, reason="Job deleted")
        if job.work_root.exists():
            shutil.rmtree(job.work_root, ignore_errors=True)

    async def cleanup_stale(self, ttl_minutes: int = 60) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
        stale = [jid for jid, job in self.jobs.items() if job.created_at < cutoff]
        for jid in stale:
            await self.delete_job(jid)

        # also sweep orphan folders that are older than ttl on disk
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
            else:
                cutoff = datetime.now(timezone.utc) - timedelta(
                    minutes=self.cleanup_ttl_minutes
                )
                for path in self.base_dir.iterdir():
                    if path.is_dir() and path.stat().st_mtime < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


job_manager = JobManager()


# ---------------------------------------------------------------------------
# JMComic log bridge
# ---------------------------------------------------------------------------
def _jm_log_bridge(topic: str, msg: str) -> None:
    job_id = _current_job.get()
    formatted = f"[{topic}] {msg}"
    logger.info(formatted)
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


def _find_first_image(chapter_dir: Path) -> Optional[Path]:
    if not chapter_dir.exists():
        return None
    for path in sorted(chapter_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in IMG_SUFFIXES:
            return path
    return None


def _zip_chapter(src_dir: Path, dest_cbz: Path) -> None:
    dest_cbz.parent.mkdir(parents=True, exist_ok=True)
    import zipfile

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

    option = JmOption.default()
    option.client.impl = "api"
    option.download.cache = True
    raw_dir = job.temp_dir / "chapters"
    option.dir_rule = DirRule(rule="Bd / {Pindextitle}", base_dir=str(raw_dir))

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
    try:
        album, downloader = await asyncio.to_thread(
            download_album, job.album_id, option
        )
    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        monitor_task.cancel()
        _current_job.reset(token)
        await job_manager.delete_job(job.id)
        return
    except Exception as exc:  # pragma: no cover - network dependent
        job.status = JobStatus.FAILED
        job.message = f"下载失败: {exc}"
        await job_manager.broadcast(job.id, {"type": "error", "message": job.message})
        monitor_task.cancel()
        _current_job.reset(token)
        await job_manager.delete_job(job.id)
        return
    finally:
        _current_job.reset(token)

    monitor_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await monitor_task

    # Post-processing
    try:
        await asyncio.to_thread(_post_process, job, album, option, raw_dir)
    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        await job_manager.delete_job(job.id)
        return
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.message = f"后处理失败: {exc}"
        await job_manager.broadcast(job.id, {"type": "error", "message": job.message})
        await job_manager.delete_job(job.id)
        return

    job.status = JobStatus.COMPLETED
    job.progress = 1.0
    await job_manager.broadcast(
        job.id,
        {
            "type": "ready",
            "download": f"/api/jobs/{job.id}/download",
            "job": job.to_dict(),
            "message": "打包完成",
        },
    )

    if job_manager.loop:
        job_manager.loop.call_later(
            job_manager.cleanup_ttl_minutes * 60,
            lambda: asyncio.create_task(job_manager.delete_job(job.id))
            if job_manager.loop.is_running()
            else None,
        )

    job_manager.job_tasks.pop(job.id, None)


def _post_process(
    job: Job, album: JmAlbumDetail, option: JmOption, raw_dir: Path
) -> None:
    safe_title = _safe_name(album.name)
    final_dir = job.work_root / safe_title

    # cover
    first_photo: JmPhotoDetail = album[0]
    first_dir = Path(option.dir_rule.decide_image_save_dir(album, first_photo))
    first_img = _find_first_image(first_dir)
    if first_img:
        cover_path = job.temp_dir / "cover.webp"
        cover_path.write_bytes(first_img.read_bytes())
    else:
        cover_path = job.temp_dir / "cover.webp"
        cover_path.write_text("", encoding="utf-8")

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
    for photo in album:
        chapter_dir = Path(option.dir_rule.decide_image_save_dir(album, photo))
        if not chapter_dir.exists():
            continue
        chapter_name = _safe_name(
            photo.indextitle if hasattr(photo, "indextitle") else photo.name
        )
        cbz_path = job.temp_dir / f"{chapter_name}.cbz"
        _zip_chapter(chapter_dir, cbz_path)

    # Remove raw images to keep final zip small
    if raw_dir.exists():
        shutil.rmtree(raw_dir, ignore_errors=True)

    # Rename temp dir to comic title
    job.temp_dir.rename(final_dir)
    job.temp_dir = final_dir
    job.final_dir = final_dir

    # Zip whole folder
    zip_path = job.work_root / f"{safe_title}.zip"
    _zip_folder(final_dir, zip_path)
    job.zip_path = zip_path

    # Keep工作目录干净：仅保留最终 zip
    if final_dir.exists():
        shutil.rmtree(final_dir, ignore_errors=True)
    job.final_dir = None
    job.temp_dir = job.work_root


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
async def lifespan(app: FastAPI):
    job_manager.set_loop(asyncio.get_running_loop())
    await job_manager.cleanup_stale(ttl_minutes=job_manager.cleanup_ttl_minutes)
    yield
    await job_manager.cleanup_stale(ttl_minutes=0)


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
    job = job_manager.get(job_id)
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
        job = await job_manager.attach_ws(job_id, websocket)
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
