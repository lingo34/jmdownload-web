# Repository Guidelines

## Project Structure & Module Organization
- `main.py`: FastAPI app plus `JobManager` orchestrating JMComic downloads, progress WebSockets, zip creation, and temp cleanup. Most backend logic lives here; keep functions async-friendly.
- `static/index.html`: Single-page UI that calls the REST endpoints and `/ws/{job_id}` WebSocket; update this directly (no build pipeline).
- Container assets live in `Dockerfile` and `docker-compose.yml`. Generated folders such as `__pycache__/`, `build/`, and `void_halo.egg-info/` should stay untracked.

## Setup, Build, and Development Commands
- Python 3.13+ recommended. Create an env and install deps:
  ```bash
  python -m venv .venv && source .venv/bin/activate
  pip install --upgrade pip
  pip install "fastapi[standard]>=0.124.0" "jmcomic>=2.6.10" "standard-imghdr>=3.13.0" "uvicorn>=0.38.0" "watchdog>=6.0.0"
  ```
- Run locally with reload (default port 7392):  
  `python -m uvicorn main:app --reload --host 0.0.0.0 --port 7392`
- Docker flow: `docker build -t void-halo:dev .` then `docker run --rm -p 7392:7392 -e VOID_HALO_BASEDIR=/tmp/void-halo void-halo:dev`
- docker-compose example: `docker-compose up --build`

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indentation and type hints. Use `snake_case` for functions/vars and `PascalCase` for classes.
- Keep logging via the module-level `logger` and prefer structured/status messages that surface in WebSocket events.
- Avoid blocking the event loop; long work should stay in background tasks or threads.
- Frontend: keep vanilla JS/CSS in `static/index.html`; align new strings with existing Simplified Chinese UI copy.

## Testing Guidelines
- No formal suite yet; add `tests/` using `pytest` (and `pytest-asyncio` for WebSocket/API flows). Example: `pytest tests/test_jobs.py -q`
- Cover happy-path album download, failure cases (invalid ID, network errors), and cleanup of `VOID_HALO_BASEDIR`.
- For manual smoke tests: run the dev server, open `http://127.0.0.1:7392`, start a job, verify progress pushes, download zip, then click “删除文件” to confirm cleanup.

## Commit & Pull Request Guidelines
- Use Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`) to keep history readable.
- PRs should include: purpose/summary, touched endpoints or UI areas, config/env changes, test results (automated or manual steps), and before/after screenshots for UI tweaks.
- Keep diffs minimal; do not commit generated artifacts or local virtualenv files.

## Deployment & Configuration Tips
- Set `VOID_HALO_BASEDIR=/path` to persist downloads across restarts; otherwise a temp dir is used and cleaned on exit.
- Default port is 7392; update compose/ingress accordingly if changed.
- If you adjust build args or tags, mirror the changes in `.github/workflows/publish.yml` to keep GHCR pushes consistent.
