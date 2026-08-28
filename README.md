# 黔衣有话

贵州文旅博主 AI 内容资产与运营辅助产品。

## 第一阶段目标

完成“画像采集 → 可信种子建库 → DeepSeek 生成 → 本地中文向量化 → 混合检索 → 决策留痕 → 长短期 Agent 记忆”的可演示基础闭环。

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

## 第一阶段数据规则

- 博主采用可审计软删除。关联资产、地点、任务、决策和记忆历史继续保留，但所有默认业务入口都拒绝访问已删除博主；第一阶段不开放恢复 API。
- 地点的 `est_cost`、`est_benefit`、`like_level`、`fits_koc`、`fits_shoot` 只有在用户明确提供或可信来源明确记载时才赋值，未知值在数据库和 API 中均保持 `null`。
- 资产检索支持 `q`、`lib_type`、`category`、独立 `tags`、`source_type`、`source`、`min_credibility`、`max_credibility`、`page`、`page_size` 组合使用。
- 当前 Alembic head 为 `0002_phase1_closure`；迁移同时支持空库 `base → head` 和已有 `0001_phase1_initial → 0002_phase1_closure`。

## 测试与质量门禁

- 全量测试与覆盖率：

  `E:\Anaconda\envs\DL\python.exe -m pytest -q --cov=app\services --cov-report=term-missing --cov-fail-under=80 --basetemp E:\Guikesong\.pytest-run`

- 静态检查：

  `E:\Anaconda\envs\DL\python.exe -m ruff check app tests migrations`

  `E:\Anaconda\envs\DL\python.exe -m mypy app`

- 性能验收：

  `E:\Anaconda\envs\DL\python.exe -m pytest tests\test_performance.py -q -s --basetemp E:\Guikesong\.pytest-performance`

- 默认离线、显式启用的真实集成 smoke：

  `set RUN_REAL_EMBEDDING=1 && E:\Anaconda\envs\DL\python.exe -m pytest tests\test_real_integrations.py -q -rs`

  `set RUN_REAL_DEEPSEEK=1 && E:\Anaconda\envs\DL\python.exe -m pytest tests\test_real_integrations.py -q -rs`

  未设置开关时不会加载真实模型或调用外部 API。真实 DeepSeek smoke 仅在本地密钥和网络均可用时执行，测试不会输出密钥。

## 运行时数据边界

以下内容均被 Git 忽略：DeepSeek 密钥、SQLite 数据库、`data/tasks/` 任务日志、本地模型权重、测试缓存和覆盖率文件。

## 当前进度

详见 [开发进度](docs/DEVELOPMENT_PROGRESS.md)。
