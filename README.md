# dlmodel

面向国内网络的模型下载与管理工具：Web UI + FastAPI + aria2，支持从 **ModelScope / Hugging Face（镜像）/ Ollama** 拉取权重，落盘到 **vLLM 布局** 或 **Ollama**；任务与设置持久化到 SQLite，可选钉钉 / 飞书 / 企业微信推送。

| | |
|--|--|
| 开发入口 | [http://127.0.0.1:8080](http://127.0.0.1:8080)（compose 默认只绑本机） |
| 生产镜像 | `swr.cn-east-3.myhuaweicloud.com/sreyun/dlmodel:latest` |
| 鉴权 | `.env` 中的 `ADMIN_TOKEN`（Bearer） |

## 功能概览

- **下载**：自动源（优先魔搭，回落 HF 镜像）、指定 ModelScope / HF / Ollama；队列并发、进度 / 速率 / ETA
- **落盘**：HF / vLLM → `{MODEL_ROOT}/hf/<org>/<repo>/`；Ollama → `{MODEL_ROOT}/ollama`
- **持久化**：SQLite（`{DATA_DIR}/app.db`）；优雅重启时进行中的任务会 **停放并恢复**，不会被取消
- **设置**：HF / ModelScope Token、并发与镜像地址；消息推送 Webhook（脱敏回显，明文不回传）
- **可选推理**：Compose profile 挂载同目录的 Ollama / vLLM（默认不启动）

## 快速开始（开发）

```bash
cp .env.example .env
# 将 ADMIN_TOKEN 改成足够长的随机串
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

浏览器打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)，用 `ADMIN_TOKEN` 登录。

开发 overlay 会本地构建 `dlmodel:dev`，并将 Web 端口绑到 `127.0.0.1`；可用 `ALLOW_INSECURE_ADMIN=1` 方便短 Token（**勿用于公网**）。

## 生产部署（华为云 SWR）

每个 `v*` 标签会触发 GitHub Actions，推送镜像：

- `swr.cn-east-3.myhuaweicloud.com/<namespace>/dlmodel:<tag>`
- `…/dlmodel:latest`（滚动更新推荐）

```bash
cp .env.example .env          # 设置强 ADMIN_TOKEN；不要开 ALLOW_INSECURE_ADMIN
export DLMODEL_IMAGE=swr.cn-east-3.myhuaweicloud.com/sreyun/dlmodel:latest
docker login swr.cn-east-3.myhuaweicloud.com
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

需要可回滚时改用版本号，例如 `:v0.3.2`。`pull_policy: always` 会在每次 `up` 时拉取最新镜像。

也可写入 `.env`：

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
DLMODEL_IMAGE=swr.cn-east-3.myhuaweicloud.com/sreyun/dlmodel:latest
```

### 发布与 CI

仓库 Secrets：`HW_ACCESS_KEY`、`HW_SECRET_KEY`。可选 Variable / Secret：`HW_SWR_NAMESPACE`（默认 `sreyun`）。

请先在 [SWR 控制台](https://console.huaweicloud.com/swr)（**华东-上海一 / cn-east-3**）创建组织，以便首次推送创建 `dlmodel` 仓库。

```bash
git tag -a v0.3.2 -m "v0.3.2"
git push origin v0.3.2
# → 自动构建并推送 :v0.3.2 与 :latest
```

手动重跑：Actions → **Build and push Huawei SWR** → Run workflow。

## 国内默认

`.env.example` 面向大陆网络：

| 项 | 说明 |
|----|------|
| `HF_ENDPOINT=https://hf-mirror.com` | HF Hub 走镜像 |
| 源 `auto` | 先 ModelScope，再 HF |
| aria2 | `ARIA2_RPC_URL=http://aria2:6800/jsonrpc`；RPC 失败时回退 HTTP Range + SSL 重试 |
| `DOWNLOAD_RETRIES` | 瞬时 TLS / 网络错误额外重试（默认 `3`） |

门禁或提速可在 `.env` 或设置页配置 `HF_TOKEN` / `MODELSCOPE_API_TOKEN`（UI 写入 SQLite，优先于环境变量中的同名合并项）。

## 数据与目录

| 主机路径 | 容器路径 | 用途 |
|----------|----------|------|
| `./data/models` | `/models`（`MODEL_ROOT`） | 模型权重 |
| `./data/app` | `/data`（`DATA_DIR`） | SQLite `app.db`、设置 |
| `./data/aria2` | `/config` | aria2 配置 |

`web` 与 `aria2` 共享模型目录。默认 `PUID=0` / `PGID=0`，保证 aria2 可写同一卷；若宿主机目录权限过严，需保证容器用户可写，否则会退化为单流 HTTP。

磁盘布局：

- vLLM / HF：`{MODEL_ROOT}/hf/<org>/<repo>/`
- Ollama（profile）：主机 `./data/models/ollama` → 容器 `/root/.ollama`

任务记录请在 UI 删除，或自行清理上述数据目录。

## 消息推送

设置 → **消息推送**：钉钉 / 飞书 / 企业微信群机器人（仅 HTTPS Webhook，主机白名单校验）。

- 默认：下载完成、失败
- 可选：开始下载、任务取消（「开始」在出现可测速进度后再发，含进度 / 速率 / ETA）
- 刷新后输入框留空表示「不修改」；页面展示脱敏地址表示已保存
- 也可用环境变量 `NOTIFY_*`；更推荐在 UI 保存

## 可选推理 Profile

管理与下载不依赖推理服务；需要时再启：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile ollama up -d --build
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile vllm up -d --build
```

| Profile | 本机 API | 说明 |
|---------|----------|------|
| `ollama` | `http://127.0.0.1:11434` | Web 使用 `OLLAMA_BASE_URL=http://ollama:11434` |
| `vllm` | `http://127.0.0.1:8000` | OpenAI 兼容；需 NVIDIA Container Toolkit |

vLLM 注意：

- 镜像默认 `vllm/vllm-openai:latest` + `runtime: nvidia`，可按主机改镜像 / 设备
- `VLLM_MODEL` 必须是**已下载的具体仓库路径**（如 `/models/hf/org/repo`），不能是 `/models/hf` 根目录；下载完成后再写入 `.env` 并 recreate `vllm`
- `HUGGING_FACE_HUB_TOKEN` 由 `HF_TOKEN` 传入（引擎仍需拉 Hub 时）

## 配置参考

详见 `.env.example`。常用变量：

| 变量 | 说明 |
|------|------|
| `ADMIN_TOKEN` | UI / API Bearer |
| `MODEL_ROOT` / `DATA_DIR` | 模型与 SQLite |
| `DLMODEL_IMAGE` | 生产镜像（推荐 `:latest`，回滚钉 `:vX.Y.Z`） |
| `HF_ENDPOINT` / `HF_TOKEN` | HF 镜像与 Token |
| `MODELSCOPE_API_TOKEN` | 魔搭 Token |
| `ARIA2_RPC_URL` / `ARIA2_RPC_SECRET` | 须与 `aria2` 服务一致 |
| `DOWNLOAD_CONCURRENCY` / `ARIA2_CONNECTIONS` | 任务并发 / 单文件连接数 |
| `DOWNLOAD_RETRIES` | SSL / 网络瞬时错误重试 |
| `NOTIFY_*` | 群机器人 Webhook 与事件开关 |
| `OLLAMA_BASE_URL` / `VLLM_BASE_URL` | 推理地址（profile 或远程） |
| `VLLM_MODEL` | 可选 `vllm` 服务的模型路径 |

## 本地运行（不用 Docker）

```bash
cp .env.example .env
cd backend
pip install -e ".[dev]"
# 将 MODEL_ROOT / DATA_DIR / ARIA2_RPC_URL 指到本机路径
uvicorn app.main:app --reload --port 8080
```

API 从仓库根目录的 `frontend/` 提供静态页（`backend/app/main.py` → `../frontend`）。Docker 镜像将前端拷到 `/frontend`，并设置 `FRONTEND_DIR=/frontend`。

测试：

```bash
cd backend && python -m pytest tests/ -q
```

## Compose 文件一览

| 文件 | 用途 |
|------|------|
| `docker-compose.yml` | 基础栈（web + aria2 + 可选 profile） |
| `docker-compose.dev.yml` | 本地构建、本机绑定、宽松管理 Token |
| `docker-compose.prod.yml` | 拉取 SWR 镜像、健康检查、日志轮转、可对外端口 |
