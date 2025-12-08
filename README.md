# Void Halo

一个基于 FastAPI + JMComic 的漫画打包下载工具。

## 快速开始

```bash
# 创建虚拟环境（可选）
python -m venv .venv && source .venv/bin/activate

# 安装依赖
pip install --upgrade pip
pip install "fastapi[standard]>=0.124.0" "jmcomic>=2.6.10" "standard-imghdr>=3.13.0" "uvicorn>=0.38.0" "watchdog>=6.0.0"

# 开发运行（默认端口 7392）
python -m uvicorn main:app --reload --host 0.0.0.0 --port 7392
```

打开浏览器访问 http://127.0.0.1:7392 即可使用。

- 填入漫画 ID，点击开始后，后端会下载漫画、提取封面、写出 `details.json`、按章节打包为 `.cbz`，最终生成一个总 zip 供下载。
- 前端通过 WebSocket 推送实时日志和进度。
- 点击“删除文件”或关闭页面后可清理临时目录（默认存放在系统临时目录的 `void-halo/` 下）。

> 需要 Python 3.13+。

## Docker 运行

直接使用 GHCR 镜像（由 GitHub Actions 自动构建）：

```bash
docker run --rm -p 7392:7392 ghcr.io/<your-gh-username>/void-halo:latest
```

docker-compose 示例（可自定义工作目录/挂载 tmpfs）：

```yaml
services:
  void-halo:
    image: ghcr.io/${GHCR_OWNER:-your-gh-username}/void-halo:latest
    ports:
      - "7392:7392"
    environment:
      - VOID_HALO_BASEDIR=/tmp/void-halo
    volumes:
      - /tmp/void-halo:/tmp/void-halo
    restart: unless-stopped
```

## CI 镜像发布

`.github/workflows/publish.yml` 在 push 到 `main`/`master` 或打 `v*` 标签时，使用内置 `GITHUB_TOKEN` 构建并推送镜像到 `ghcr.io/<org or user>/void-halo`（latest + sha 标签）。

本地构建：

```bash
docker build -t void-halo:local .
docker run -p 7392:7392 void-halo:local
```
