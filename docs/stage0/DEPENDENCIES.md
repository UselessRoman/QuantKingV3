# S0-01 锁定依赖记录

状态：2026-07-15 已按 `uv.lock`、`frontend/package-lock.json` 和本机已安装包元数据记录。
版本表是 S0-01 的锁定结果，不代表 S0-02 联合兼容性探针已经通过。升级任何直接依赖时，
必须重新锁定、更新本表并重跑完整质量门。

## Python 运行依赖

下表各版本均已安装在 Windows x86-64 / Python 3.12.13 的统一 `.venv`；Qlib、
LightGBM、Backtrader、DuckDB 和 FastAPI 的功能级联合探针仍属于 S0-02。

| 依赖 | 锁定版本 | 用途 | 维护状态与许可证 | Windows/Python 3.12 | 离线影响 | 替代方案 |
|---|---:|---|---|---|---|---|
| Alembic | 1.18.5 | SQLite schema 迁移 | 活跃稳定；MIT | 已锁定安装 | 首次安装需 wheel；运行迁移离线 | SQLAlchemy 手写迁移（可审计性较弱） |
| Backtrader | 1.9.78.123 | 正式事件驱动回测验收 | 成熟、发布频率低；GPL-3.0+ | 纯 Python 包已安装，S0-02 探针待做 | 包缓存后离线 | 自研事件引擎（成本高） |
| DuckDB | 1.5.4 | 本地批量查询 | 活跃稳定；MIT | Windows wheel 已安装 | wheel 较大，运行离线 | PyArrow/Pandas 查询（能力或性能不同） |
| FastAPI | 0.139.0 | 本机同源 API 入口 | 活跃稳定；MIT | 已安装 | 运行离线 | Starlette（需要更多自建 schema/文档） |
| LightGBM | 4.6.0 | Stage 3 模型基线，Stage 0 兼容性探针 | 活跃稳定；MIT | Windows wheel 已安装 | 原生 wheel 较大，训练离线 | scikit-learn HistGradientBoosting |
| pydantic-settings | 2.14.2 | 显式配置模型支持 | 活跃稳定；MIT | 已安装 | 运行离线 | 标准库 TOML + dataclass 手工校验 |
| pyqlib | 0.9.7 | 研究数据、模型和快速回测适配器 | 稳定发行版；MIT | 已安装，S0-02 扩展点探针待做 | 传递依赖多、离线缓存体积大 | 自有研究管线（不满足已确认 Qlib 决策） |
| SQLAlchemy | 2.0.51 | SQLite 持久化实现 | 活跃稳定；MIT | 已安装 | 运行离线 | `sqlite3` 手写仓储（迁移/类型成本更高） |
| Uvicorn | 0.51.0 | 仅回环地址的 ASGI 入口 | 活跃稳定；BSD-3-Clause | 已安装 | 运行离线 | Hypercorn |

`pyqlib` 0.9.7 的上游传递依赖包含 `redis` 与 `python-redis-lock` Python 客户端。
QuantKing 没有把它们声明为直接依赖，不部署或连接 Redis，也不允许应用代码使用 Redis；
Stage 0 的队列仍按架构决策使用 SQLite。锁文件中没有 Celery。

## Python 开发与质量依赖

| 依赖 | 锁定版本 | 用途 | 维护状态与许可证 | Windows/Python 3.12 | 离线影响 | 替代方案 |
|---|---:|---|---|---|---|---|
| HTTPX | 0.28.1 | FastAPI 测试客户端 | 活跃稳定；BSD-3-Clause | 已安装 | 测试运行离线 | Starlette/ASGI 自定义测试调用 |
| mypy | 1.20.2 | 严格静态类型检查 | 活跃稳定；MIT | 已安装 | 运行离线 | Pyright（会引入第二套规则） |
| pytest | 8.4.2 | Python 测试运行器 | 活跃稳定；MIT | 已安装 | 运行离线 | `unittest`（fixture/marker 能力较弱） |
| pytest-timeout | 2.4.0 | 并发测试硬超时 | 活跃稳定；MIT | 已安装 | 运行离线 | 测试内手工超时 |
| Ruff | 0.15.21 | lint 与格式门禁 | 活跃稳定；MIT | Windows 可执行包已安装 | 运行离线 | Black + isort + Flake8（工具更多） |

构建后端 Hatchling 使用 `>=1.27,<2` 范围，许可证 MIT。`uv` 当前不会把隔离构建后端
写入应用 `uv.lock`，因此它不冒充“已锁定的运行依赖”；干净环境构建仍需已有 Hatchling
缓存或经授权下载。这是保留的离线构建限制。

## 前端直接依赖

所有版本都由 `frontend/package-lock.json` 精确固定，并已安装在 Node 24.18.0 / npm
11.16.0 的现有 `node_modules`。React 固定在 19.2 稳定线。

| 依赖 | 锁定版本 | 用途 | 维护状态与许可证 | Node 24 | 离线影响 | 替代方案 |
|---|---:|---|---|---|---|---|
| React | 19.2.7 | S0-04 UI 运行时 | 活跃稳定线；MIT | 已安装 | 构建/运行离线 | Preact（与已确认栈不同） |
| React DOM | 19.2.7 | 浏览器渲染 | 活跃稳定线；MIT | 已安装 | 构建/运行离线 | Preact compat |
| Playwright Test | 1.61.1 | 关键浏览器 E2E | 活跃稳定；Apache-2.0 | 声明 Node >=18，已安装 | 浏览器二进制需另行缓存；S0-01 无测试通过不启动浏览器 | WebdriverIO |
| Testing Library jest-dom | 6.9.1 | DOM 断言 | 活跃稳定；MIT | 声明 Node >=14，已安装 | 运行离线 | 原生 DOM 断言 |
| Testing Library React | 16.3.0 | React 组件测试 | 活跃稳定；MIT | 声明 Node >=18，已安装 | 运行离线 | React Test Renderer |
| Testing Library user-event | 14.6.1 | 用户交互模拟 | 活跃稳定；MIT | 声明 Node >=12，已安装 | 运行离线 | `fireEvent`（语义较弱） |
| React 类型 | 19.2.7 | React TypeScript 类型 | 随 React 维护；MIT | 已安装 | 编译离线 | 本地声明（不推荐） |
| React DOM 类型 | 19.2.3 | React DOM TypeScript 类型 | 随 React 维护；MIT | 已安装 | 编译离线 | 本地声明（不推荐） |
| Vite React 插件 | 5.1.1 | React 转换与刷新 | 活跃稳定；MIT | 声明支持 >=22.12，Node 24 满足 | 构建离线 | 手工 Babel/SWC 配置 |
| jsdom | 27.3.0 | Vitest DOM 环境 | 活跃稳定；MIT | 明确支持 Node >=24 | 运行离线 | happy-dom |
| TypeScript | 5.9.3 | 类型检查与编译 | 活跃稳定；Apache-2.0 | 声明 Node >=14.17，已安装 | 编译离线 | 无等价替代且不改变已确认栈 |
| Vite | 7.3.6 | 开发与生产构建 | 活跃稳定；MIT | 声明支持 >=22.12，Node 24 满足 | 构建离线 | webpack/Rspack |
| Vitest | 4.1.10 | 前端单元测试 | 活跃稳定；MIT | 明确支持 Node >=24 | 运行离线 | Jest |

## XTQuant 和外部边界

`xtquant` 是 QMT 安装附带的外部 SDK，不从 PyPI 伪造或写入 `pyproject.toml`/`uv.lock`。
它只能由 `data/adapters` 或 `trading/adapters` 在经授权的真实探针中导入；默认测试、
依赖安装和 S0-01 质量门都不连接 XTQuant、QMT、账户或网络。
