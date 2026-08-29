# 贵客松 · 贵州文旅创作智能体

面向贵州地方 IP 的内容生产工作台。它把知识库、素材库、平台算法库、创作者画像、选题策划、脚本与分镜、发布排期、经营复盘串成一条可交互的前端工作流。

## 当前能力

- 对话式完善博主/IP 画像
- 普通问题通过后端调用真实 DeepSeek 多轮对话，不使用前端写死回复
- 三库检索、录入、删除与可信度标记
- Agent 健康度和内容生产准备度评估
- 根据选题生成脚本、分镜和来源卡
- 按预估收益排序文旅路线
- 内容日历与模拟发布
- 流量、利润与 Agent 进化闭环看板
- 决策日志，展示系统为什么给出建议

> 当前版本为前后端分离的演示产品。普通对话、画像、三库、体检和脚本链路通过后端 API 工作；发布和经营数据仍保留明确的模拟来源，不调用真实内容平台。

## 本地运行

前端不会读取 DeepSeek Key。开发环境通过 Vite 将 `/api/v1` 请求代理到本地 FastAPI 后端，默认目标为 `http://127.0.0.1:8000`。

先启动后端：

```powershell
cd E:\Guikesong
E:\Anaconda\envs\DL\python.exe -m pip install -e ".[dev]"
E:\Anaconda\envs\DL\python.exe -m alembic upgrade head
E:\Anaconda\envs\DL\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

DeepSeek Key 只放在后端工作区根目录的 `deepseek_apikey.txt`，不要复制到前端目录或提交到 Git。

再启动前端：

```bash
cd guikesong-creator-agent-main
npm install
npm run dev
```

打开 [http://localhost:3000](http://localhost:3000)。

生产构建：

```bash
npm run build
npm start
```

如果后端不是运行在 `127.0.0.1:8000`，可以在启动前设置代理地址：

```powershell
$env:API_PROXY_TARGET = "http://127.0.0.1:8000"
npm run dev
```

也可以复制 `.env.example` 为 `.env.local`，通过 `NEXT_PUBLIC_AGENT_API_BASE` 指定可直接访问的后端 API 地址。生产环境必须由部署层配置 CORS、HTTPS 和密钥，前端环境变量中不得放置模型密钥。

## API 对接

- 前端适配层：`app/lib/agent-api.ts`
- 默认 API 前缀：`/api/v1`
- 普通多轮对话：`POST /bloggers/{blogger_id}/chat`
- 默认演示博主：`id=2`
- 画像、三库、体检、脚本使用后端对应资源接口

聊天请求只发送当前消息和最近的对话上下文，后端负责读取 SQLite 中的博主基础信息、调用 DeepSeek，并返回结构化错误码。前端不会绕过后端直连 DeepSeek。

直接指定后端地址时，配置格式为：

```env
NEXT_PUBLIC_AGENT_API_BASE=http://127.0.0.1:8000/api/v1
```

## 技术栈

- Next.js 16 App Router / vinext
- React 19 + TypeScript 5.9
- Vite 8、React Server Components 及 Cloudflare Vite 插件
- CSS 原生响应式布局
- 后端 FastAPI + SQLAlchemy + SQLite + Alembic
- DeepSeek Chat Completions API（由后端统一调用）

## 目录结构

```text
app/
  page.tsx              # 工作台页面、会话状态和意图分流
  globals.css           # 页面样式
  lib/agent-api.ts      # 前端到后端的请求适配层
public/                 # 前端静态资源
vite.config.ts          # 开发代理和 vinext 配置
```

## 验证

- `npm run build`
- `npx tsc --noEmit`
- 桌面与移动端响应式页面检查
- 主流程交互检查：画像 → 三库 → 体检 → 脚本
- 对话检查：普通聊天、连续追问、后端错误展示
- 主流程交互检查：知识录入 → 诊断 → 脚本/分镜 → 路线 → 排期 → 复盘

## 安全边界

- `.env*`、`*.key`、`*_apikey.txt` 不进入 Git。
- 前端只保存浏览器会话展示状态，不保存 DeepSeek Key。
- SQLite 数据库、运行时任务、模型缓存、测试缓存和构建产物不进入 Git。
- 真实平台发布和经营数据采集仍未接入，相关页面保留模拟来源标识。

## 许可

本仓库用于“贵客松”赛事项目展示与后续迭代。
