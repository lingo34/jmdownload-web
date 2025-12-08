# Void Halo

一个基于 FastAPI + JMComic 的漫画打包下载工具。

## 快速开始

```bash
uv run main.py
```

打开浏览器访问 http://127.0.0.1:8000 即可使用。

- 填入漫画 ID，点击开始后，后端会下载漫画、提取封面、写出 `details.json`、按章节打包为 `.cbz`，最终生成一个总 zip 供下载。
- 前端通过 WebSocket 推送实时日志和进度。
- 点击“删除文件”或关闭页面后可清理临时目录（默认存放在系统临时目录的 `void-halo/` 下）。

> 需要 Python 3.13+。依赖已写在 `pyproject.toml`，使用 Astral UV（`uv run` / `uv sync`）。
