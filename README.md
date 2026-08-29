# 黔衣有话

贵州文旅博主 AI 内容资产与运营辅助产品。

## 技术基线

- 后端：Python、FastAPI、SQLAlchemy、SQLite
- 演示页面：Jinja2 + 原生 JavaScript
- 大模型：DeepSeek `deepseek-v4-flash`
- 本地向量模型：`BAAI/bge-small-zh-v1.5`，GPU 优先、CPU 降级
- 开发环境：Conda `DL`

## 本地启动

1. 安装项目依赖：

   `E:\Anaconda\envs\DL\python.exe -m pip install -e ".[dev]"`

2. 首次使用时把中文向量模型下载到本地缓存：

   `E:\Anaconda\envs\DL\python.exe -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5', cache_folder='models')"`

3. 将 DeepSeek Key 放在工作区根目录 `deepseek_apikey.txt`，该文件已被 Git 忽略且不会写入数据库或日志。

4. 初始化数据库：

   `E:\Anaconda\envs\DL\python.exe -m alembic upgrade head`

5. 使用 DL 环境启动：

   `E:\Anaconda\envs\DL\python.exe -m uvicorn app.main:app --reload`

6. 浏览器访问 `http://127.0.0.1:8000`。页面只用于功能演示，不包含视觉设计。

## 核心功能

- 通过自然语言多轮对话采集和维护博主画像。
- 使用真实 SQLite 数据完成画像、三库、体检和脚本生成链路。
- 支持素材、地点、平台规则、脚本输出和证据引用管理。
- 支持体检、脚本生成、重试、历史输出和结构化错误反馈。
- 前后端分离：前端只调用 API，DeepSeek Key 仅由后端读取。

## API 概览

- `POST /api/v1/bloggers/{blogger_id}/chat`：普通多轮对话。
- `POST /api/v1/bloggers/{blogger_id}/assessments`：创建体检。
- `POST /api/v1/bloggers/{blogger_id}/outputs/generate/script`：生成脚本。
- `GET /api/v1/bloggers/{blogger_id}/outputs`：查询脚本输出历史。

完整接口以运行中的 FastAPI OpenAPI 文档为准：`http://127.0.0.1:8000/docs`。

## 测试与质量门禁

```powershell
E:\Anaconda\envs\DL\python.exe -m pytest -q
E:\Anaconda\envs\DL\python.exe -m ruff check app tests migrations
E:\Anaconda\envs\DL\python.exe -m mypy app
```

真实模型测试需要显式设置对应的环境变量，并且只在本地密钥和网络可用时运行。

## 运行时数据边界

以下内容均被 Git 忽略：DeepSeek 密钥、SQLite 数据库、`data/tasks/` 任务日志、本地模型权重、测试缓存和覆盖率文件。

## 前端工作台

前端位于 [`guikesong-creator-agent-main`](guikesong-creator-agent-main/)，与 FastAPI 后端保持独立部署。前端采用 Next.js App Router、React、TypeScript、Vite/vinext 和原生 CSS；后端采用 Python、FastAPI、SQLAlchemy、SQLite、Alembic，并由后端统一调用 DeepSeek。

前端默认请求 `/api/v1`，本地开发服务器通过 `API_PROXY_TARGET` 代理到 `http://127.0.0.1:8000`。普通多轮对话使用 `POST /api/v1/bloggers/{blogger_id}/chat`，默认演示博主为 `id=2`；画像、三库、体检、脚本继续使用对应的后端资源接口。完整前端启动、接口和验证说明见 [`guikesong-creator-agent-main/README.md`](guikesong-creator-agent-main/README.md)。

## 发布安全检查

提交前必须确认以下内容没有被加入暂存区：`deepseek_apikey.txt`、`.env*`、SQLite 数据库、`data/`、本地模型缓存、测试缓存、日志和构建产物。仓库根目录及前端目录的 `.gitignore` 已覆盖这些本地文件；DeepSeek Key 只从后端本地文件读取，不在前端代码中保存。
