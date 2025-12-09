"""Unit tests for the Void Halo JMComic downloader.

Run with: uv run pytest tests/ -v
"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def mock_album_detail():
    """Create a mock JmAlbumDetail object."""
    album = MagicMock()
    album.name = "Test Album"
    album.album_id = "123456"
    album.author = "Test Author"
    album.authors = ["Test Author"]
    album.description = "Test description"
    album.tags = ["tag1", "tag2"]
    album.page_count = 10

    # Mock photo
    photo = MagicMock()
    photo.photo_id = "123456"
    photo.name = "Test Photo"
    photo.indextitle = "Chapter 1"
    photo.index = 1

    album.__iter__ = lambda self: iter([photo])
    album.__getitem__ = lambda self, idx: photo

    return album


@pytest.fixture
def mock_album_detail_no_indextitle():
    """Create a mock JmAlbumDetail with None indextitle."""
    album = MagicMock()
    album.name = "Test Album No Index"
    album.album_id = "789012"
    album.author = "Test Author"
    album.authors = None
    album.description = None
    album.tags = []
    album.page_count = 5

    # Mock photo without indextitle
    photo = MagicMock()
    photo.photo_id = "789012"
    photo.name = None
    photo.indextitle = None
    photo.index = 1

    # Set getattr behavior to return None for these attributes
    del photo.indextitle
    del photo.name

    album.__iter__ = lambda self: iter([photo])
    album.__getitem__ = lambda self, idx: photo

    return album


# ---------------------------------------------------------------------------
# Test helper functions
# ---------------------------------------------------------------------------
class TestSafeName:
    """Tests for the _safe_name helper function."""

    def test_safe_name_normal(self):
        from main import _safe_name

        assert _safe_name("Test Album") == "Test Album"

    def test_safe_name_with_special_chars(self):
        from main import _safe_name

        # Should remove/replace invalid filename characters
        result = _safe_name("Test: Album / Story")
        assert ":" not in result or "/" not in result

    def test_safe_name_empty(self):
        from main import _safe_name

        assert _safe_name("") == "untitled"

    def test_safe_name_whitespace_only(self):
        from main import _safe_name

        assert _safe_name("   ") == "untitled"


class TestFormatSize:
    """Tests for the _format_size helper function."""

    def test_format_bytes(self):
        from main import _format_size

        assert _format_size(100) == "100 B"

    def test_format_kilobytes(self):
        from main import _format_size

        result = _format_size(1024)
        assert "KB" in result

    def test_format_megabytes(self):
        from main import _format_size

        result = _format_size(1024 * 1024)
        assert "MB" in result

    def test_format_gigabytes(self):
        from main import _format_size

        result = _format_size(1024 * 1024 * 1024)
        assert "GB" in result


class TestCoverSelection:
    """Tests for cover selection and writing."""

    def test_select_cover_prefers_00001_webp(self, temp_dir):
        from main import _select_cover_image

        root = temp_dir / "chapters"
        root.mkdir()
        (root / "b.webp").write_text("b")
        (root / "00001.webp").write_text("cover")
        (root / "a.jpg").write_text("a")

        result = _select_cover_image(root)
        assert result.name == "00001.webp"

    def test_select_cover_fallback_to_sorted_first(self, temp_dir):
        from main import _select_cover_image

        root = temp_dir / "chapters2"
        root.mkdir()
        (root / "b.png").write_text("b")
        (root / "a.webp").write_text("a")

        result = _select_cover_image(root)
        assert result.name == "a.webp"

    def test_write_cover_creates_empty_when_no_images(self, temp_dir):
        from main import _write_cover

        dest = temp_dir / "out" / "cover.webp"
        _write_cover(temp_dir, dest)

        assert dest.exists()
        assert dest.stat().st_size == 0


class TestPostProcessFromDisk:
    """Tests for partial-download post processing."""

    def test_post_process_from_disk_builds_cover_and_cbz(self, temp_dir):
        from main import Job, _post_process_from_disk

        job = Job(
            id="partial",
            album_id="1",
            work_root=temp_dir,
            temp_dir=temp_dir / "tmp",
        )
        job.temp_dir.mkdir(parents=True, exist_ok=True)

        raw_dir = job.temp_dir / "chapters"
        raw_dir.mkdir()
        chapter_dir = raw_dir / "chapter_1"
        chapter_dir.mkdir()
        (chapter_dir / "00002.webp").write_text("img")
        (chapter_dir / "00001.webp").write_text("img")  # should become cover

        _post_process_from_disk(
            job,
            raw_dir,
            "Partial Album",
            author="Author",
            artist="Artist",
            description="desc",
            tags=["t1"],
        )

        # final dir should exist with cover and cbz
        assert job.final_dir is not None
        cover = job.final_dir / "cover.webp"
        cbz = job.final_dir / "chapter_1.cbz"
        assert cover.exists()
        assert cbz.exists()


# ---------------------------------------------------------------------------
# Test data models
# ---------------------------------------------------------------------------
class TestJobStatus:
    """Tests for JobStatus enum."""

    def test_job_status_values(self):
        from main import JobStatus

        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.RUNNING.value == "running"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.COMPLETED.value == "completed"
        assert JobStatus.CANCELLED.value == "cancelled"


class TestAlbumStatus:
    """Tests for AlbumStatus enum."""

    def test_album_status_values(self):
        from main import AlbumStatus

        assert AlbumStatus.PENDING.value == "pending"
        assert AlbumStatus.DOWNLOADING.value == "downloading"
        assert AlbumStatus.COMPLETED.value == "completed"
        assert AlbumStatus.FAILED.value == "failed"


class TestAlbumInfo:
    """Tests for AlbumInfo dataclass."""

    def test_album_info_creation(self):
        from main import AlbumInfo, AlbumStatus

        info = AlbumInfo(album_id="123", title="Test Album")
        assert info.album_id == "123"
        assert info.title == "Test Album"
        assert info.status == AlbumStatus.PENDING
        assert info.progress == 0.0
        assert info.error_message is None

    def test_album_info_to_dict(self):
        from main import AlbumInfo, AlbumStatus

        info = AlbumInfo(
            album_id="123",
            title="Test Album",
            status=AlbumStatus.COMPLETED,
            progress=1.0,
        )
        d = info.to_dict()

        assert d["album_id"] == "123"
        assert d["title"] == "Test Album"
        assert d["status"] == "completed"
        assert d["progress"] == 1.0
        assert d["error_message"] is None


class TestJob:
    """Tests for Job dataclass."""

    def test_job_creation(self, temp_dir):
        from main import Job, JobStatus

        job = Job(
            id="test-id",
            album_id="123456",
            work_root=temp_dir,
            temp_dir=temp_dir / "tmp",
        )

        assert job.id == "test-id"
        assert job.album_id == "123456"
        assert job.status == JobStatus.PENDING
        assert job.progress == 0.0

    def test_job_to_dict(self, temp_dir):
        from main import Job, JobStatus

        job = Job(
            id="test-id",
            album_id="123456",
            status=JobStatus.COMPLETED,
            progress=1.0,
            title="Test Title",
            work_root=temp_dir,
            temp_dir=temp_dir / "tmp",
        )

        d = job.to_dict()
        assert d["id"] == "test-id"
        assert d["album_id"] == "123456"
        assert d["status"] == "completed"
        assert d["progress"] == 1.0
        assert d["title"] == "Test Title"
        assert d["download_ready"] is False


class TestPostProcessGuardrails:
    """Tests for post-processing edge cases."""

    def test_post_process_empty_album_raises(self, temp_dir):
        from main import Job, JmOption, _post_process, DirRule

        job = Job(
            id="empty",
            album_id="0",
            work_root=temp_dir,
            temp_dir=temp_dir / "tmp",
        )
        job.temp_dir.mkdir(parents=True, exist_ok=True)
        option = JmOption.default()
        option.dir_rule = DirRule(rule="Bd / chapter_{Pindex}", base_dir=str(temp_dir))

        album = MagicMock()
        album.name = "Empty Album"
        album.__len__.return_value = 0

        with pytest.raises(ValueError):
            _post_process(job, album, option, temp_dir / "raw")


class TestBatchJob:
    """Tests for BatchJob dataclass."""

    def test_batch_job_creation(self, temp_dir):
        from main import BatchJob, JobStatus

        job = BatchJob(id="batch-test", work_root=temp_dir)

        assert job.id == "batch-test"
        assert job.status == JobStatus.PENDING
        assert job.albums == []
        assert job.current_album_index == -1

    def test_batch_job_to_dict(self, temp_dir):
        from main import BatchJob, AlbumInfo

        job = BatchJob(
            id="batch-test",
            work_root=temp_dir,
            albums=[
                AlbumInfo(album_id="1", title="Album 1"),
                AlbumInfo(album_id="2", title="Album 2"),
            ],
        )

        d = job.to_dict()
        assert d["id"] == "batch-test"
        assert d["total_albums"] == 2
        assert d["completed_albums"] == 0
        assert len(d["albums"]) == 2


# ---------------------------------------------------------------------------
# Test JobManager
# ---------------------------------------------------------------------------
class TestJobManager:
    """Tests for JobManager class."""

    def test_create_job(self, temp_dir):
        from main import JobManager

        with patch.object(JobManager, "__init__", lambda self: None):
            manager = JobManager()
            manager.jobs = {}
            manager.job_tasks = {}
            manager.base_dir = temp_dir

            job = manager.create_job("123456")

            assert job.album_id == "123456"
            assert job.id in manager.jobs
            assert job.work_root.exists()

    def test_get_existing_job(self, temp_dir):
        from main import JobManager, Job

        with patch.object(JobManager, "__init__", lambda self: None):
            manager = JobManager()
            manager.jobs = {}

            job = Job(
                id="test-id",
                album_id="123",
                work_root=temp_dir,
                temp_dir=temp_dir,
            )
            manager.jobs["test-id"] = job

            result = manager.get("test-id")
            assert result is job

    def test_get_nonexistent_job(self, temp_dir):
        from main import JobManager

        with patch.object(JobManager, "__init__", lambda self: None):
            manager = JobManager()
            manager.jobs = {}

            with pytest.raises(KeyError):
                manager.get("nonexistent")


# ---------------------------------------------------------------------------
# Test BatchJobManager
# ---------------------------------------------------------------------------
class TestBatchJobManager:
    """Tests for BatchJobManager class."""

    def test_create_batch_job(self, temp_dir):
        from main import BatchJobManager

        manager = BatchJobManager(temp_dir)
        job = manager.create_batch_job()

        assert job.id in manager.batch_jobs
        assert job.work_root.exists()
        assert "batch_" in str(job.work_root)

    def test_get_existing_batch_job(self, temp_dir):
        from main import BatchJobManager

        manager = BatchJobManager(temp_dir)
        job = manager.create_batch_job()

        result = manager.get(job.id)
        assert result is job

    def test_get_nonexistent_batch_job(self, temp_dir):
        from main import BatchJobManager

        manager = BatchJobManager(temp_dir)

        with pytest.raises(KeyError):
            manager.get("nonexistent")

    def test_delete_batch_job_removes_external_zip(self, temp_dir):
        from main import BatchJobManager

        manager = BatchJobManager(temp_dir)
        job = manager.create_batch_job()

        external_zip = temp_dir / f"{job.work_root.name}.zip"
        external_zip.write_text("zip")
        job.zip_path = external_zip

        asyncio.run(manager.delete_job(job.id, remove_files=True))

        assert not external_zip.exists()


# ---------------------------------------------------------------------------
# Test zip functions
# ---------------------------------------------------------------------------
class TestZipFunctions:
    """Tests for zip helper functions."""

    def test_zip_chapter(self, temp_dir):
        from main import _zip_chapter

        # Create a chapter directory with some files
        chapter_dir = temp_dir / "chapter1"
        chapter_dir.mkdir()
        (chapter_dir / "image1.jpg").write_text("fake image 1")
        (chapter_dir / "image2.jpg").write_text("fake image 2")

        # Zip it
        cbz_path = temp_dir / "chapter1.cbz"
        _zip_chapter(chapter_dir, cbz_path)

        assert cbz_path.exists()

        # Verify contents
        import zipfile

        with zipfile.ZipFile(cbz_path, "r") as zf:
            names = zf.namelist()
            assert "image1.jpg" in names
            assert "image2.jpg" in names


# ---------------------------------------------------------------------------
# Test chapter name extraction
# ---------------------------------------------------------------------------
class TestChapterNameExtraction:
    """Tests for chapter name extraction with indextitle handling."""

    def test_extract_with_indextitle(self):
        from main import _safe_name

        photo = MagicMock()
        photo.indextitle = "Chapter 1 - Introduction"
        photo.name = "Photo Name"

        raw_name = (
            getattr(photo, "indextitle", None)
            or getattr(photo, "name", None)
            or "chapter_1"
        )
        chapter_name = _safe_name(str(raw_name))

        assert "Chapter 1" in chapter_name

    def test_extract_with_none_indextitle(self):
        from main import _safe_name

        photo = MagicMock(spec=[])  # No attributes

        raw_name = (
            getattr(photo, "indextitle", None)
            or getattr(photo, "name", None)
            or "chapter_1"
        )
        chapter_name = _safe_name(str(raw_name))

        assert chapter_name == "chapter_1"

    def test_extract_fallback_to_name(self):
        from main import _safe_name

        photo = MagicMock()
        photo.indextitle = None
        photo.name = "Episode 5"

        raw_name = (
            getattr(photo, "indextitle", None)
            or getattr(photo, "name", None)
            or "chapter_1"
        )
        chapter_name = _safe_name(str(raw_name))

        assert chapter_name == "Episode 5"


# ---------------------------------------------------------------------------
# Test API endpoints (integration-style)
# ---------------------------------------------------------------------------
class TestAPIEndpoints:
    """Tests for FastAPI endpoints."""

    @pytest.fixture
    def client(self):
        """Create a test client."""
        from fastapi.testclient import TestClient
        from main import app

        return TestClient(app)

    def test_health_endpoint(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_index_page(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Void Halo" in response.text

    def test_create_job_missing_album_id(self, client):
        response = client.post("/api/jobs", json={})
        assert response.status_code == 400
        assert "album_id" in response.json()["detail"]

    def test_create_batch_job_missing_credentials(self, client):
        response = client.post("/api/batch-jobs", json={})
        assert response.status_code == 400
        assert "username and password" in response.json()["detail"]

    def test_get_nonexistent_job(self, client):
        response = client.get("/api/jobs/nonexistent-id")
        # Should raise KeyError which becomes 500 or handled as 404
        assert response.status_code in (404, 500)

    def test_get_nonexistent_batch_job(self, client):
        response = client.get("/api/batch-jobs/nonexistent-id")
        assert response.status_code == 404

    def test_manual_retry_partial_marks_partial(self, client, monkeypatch):
        from main import (
            AlbumInfo,
            AlbumStatus,
            batch_job_manager,
        )

        batch_job = batch_job_manager.create_batch_job()
        album = AlbumInfo(album_id="retry-1", title="Retry Album")
        album.status = AlbumStatus.FAILED
        batch_job.albums = [album]
        batch_job.current_album_index = 0

        async def fake_download(bj, ai):
            assert bj is batch_job
            assert ai is album
            return {"failed_count": 2, "total_count": 5, "error": "partial"}

        monkeypatch.setattr(
            "main._download_single_album_for_batch",
            AsyncMock(side_effect=fake_download),
        )

        resp = client.post(
            f"/api/batch-jobs/{batch_job.id}/albums/{album.album_id}/retry"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "partial"
        assert album.status == AlbumStatus.PARTIAL
        assert album.failed_images == 2
        assert album.total_images == 5

        # cleanup
        asyncio.run(batch_job_manager.delete_job(batch_job.id, remove_files=True))
