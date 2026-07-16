# Stage 0 最终验收报告

状态：2026-07-16 全部通过，Stage 0 完成。Stage 1 未开始，也未获得实施授权。

验收日期：2026-07-16。外部验收代码版本：
`9181c576409ac01a7af8ad8abe716cb57ef726b3`。

## 锁定环境

| 组件 | 版本 |
|---|---|
| uv | 0.11.28 |
| CPython | 3.12.13 |
| Node.js | 24.18.0 |
| npm | 11.16.0 |
| SQLite | 3.53.1 |
| DuckDB | 1.5.4 |
| LightGBM | 4.6.0 |
| Backtrader | 1.9.78.123 |
| FastAPI | 0.139.0 |
| pyqlib | 0.9.7 |

`uv lock --check --offline`、`uv sync --frozen --all-groups --offline` 与
`npm ci --offline --ignore-scripts --no-audit --no-fund` 均以退出码 0 完成。

## 固定质量门

从仓库根运行：

```powershell
.\scripts\quality.ps1
```

最终复跑退出码为 `0`：Python `213 passed, 3 deselected`，Vitest `8 passed`，
真实 Microsoft Edge Playwright `1 passed`；Ruff、format、mypy、架构边界、Markdown
链接、前端类型检查与生产构建均通过。

该入口覆盖锁文件、frozen 同步、Ruff、格式、mypy、离线 pytest、架构边界、Markdown
链接、前端干净安装、类型检查、Vitest、生产构建、真实本机 Edge Playwright 和
`git diff --check`。

## 专项复核

| 项目 | 命令/范围 | 结果 |
|---|---|---|
| 六库联合探针 | `python scripts/run_compatibility_probe.py` | 同一 PID 15040；6 项全部通过；Qlib 目标权重、分数无关性与 AST 禁用内置组合构造器通过 |
| XTQuant | `pytest tests/compatibility/test_xtquant_real.py -m real_xtquant -s -q -p no:cacheprovider` | `1 passed`；零写入、零 QMT 连接、零行情读取/下载；2 条外部 SDK 弃用警告 |
| SQLite spawn | `pytest tests/concurrency/test_sqlite_spawn.py -q -p no:cacheprovider` | `2 passed in 3.38s`；4 进程 × 25 事务无丢失，双 Worker 只领取一次 |
| Worker/JSONL/REST/SSE | 任务、日志、仓储及 Web 定向集 | `44 passed`；游标恢复、原子事件、崩溃恢复和递归脱敏通过 |
| 备份完整性 | `pytest tests/backup -m "not real_backup" -q -p no:cacheprovider` | `42 passed, 1 deselected`；旧/未知 marker、报告/schema/ID 篡改均拒绝 |
| 旧项目清单 | `python scripts/inventory_legacy.py --root E:\QuantKing --dry-run` | 6,123 文件；551,400,102 字节；231 项；0 跳过；0 内容哈希；前后指纹一致 |

旧项目指纹为
`43112d62de1bcea0c6198c893da9bb3a36d69734cbd89fa8b9dad75103df3695`。
扫描只读取文件系统元数据，没有复制或修改 `E:\QuantKing`。

## 真实本地与异盘恢复

两次运行均由用户分别明确授权，只在各自新目录中创建合成数据。没有读取既有用户数据、
没有删除或覆盖、没有网络或 QMT 访问。

| 任务 | 运行号 | 结果文件 | 异盘恢复点根 |
|---|---|---|---|
| S0-05 | `stage0-s05-20260715-1920` | `E:\QuantKingV3Data\acceptance\stage0-s05-20260715-1920\acceptance-result.json` | `F:\QuantKingV3Backups\acceptance\stage0-s05-20260715-1920\store\restore_points` |
| S0-07 | `stage0-s07-20260715-1921` | `E:\QuantKingV3Data\acceptance\stage0-s07-20260715-1921\acceptance-result.json` | `F:\QuantKingV3Backups\acceptance\stage0-s07-20260715-1921\store\restore_points` |

两次结果完全满足同一验收合同：

- 首轮 `total_objects=5, copied=5, skipped=0, conflicted=0`。
- 重复轮 `total_objects=5, copied=0, skipped=5, conflicted=0`。
- 单分区变化轮 `total_objects=5, copied=1, skipped=4, conflicted=0`。
- 本地快照、本地隔离恢复与异盘隔离恢复均完成严格 manifest/SHA-256/marker 校验。
- 人工历史点、失败目录与无关哨兵：`preserved=3, deleted=0, modified=0`。
- 两次证据均记录代码版本 `9181c576409ac01a7af8ad8abe716cb57ef726b3`。

## 独立审计与边界

独立审计曾发现既有库无备份迁移、异盘复制报告未绑定、零删除证据为常量三个阻断项。
对应修复提交为 `87a3e1c` 与 `9181c57`；回归审计重新执行原攻击场景后确认三项均已解决，
定向结果为 `51 passed, 1 deselected`，未发现新的同级阻断。

质量门只剩上游弃用警告：Qlib/NumPy timedelta 3 条、FastAPI/Starlette TestClient 1 条、
XTQuant 传递模块 2 条；没有项目错误或失败测试。Stage 0 没有实现行情仓、因子、模型、组合、
回测策略、调仓、账户或交易逻辑，没有进入 Stage 1 范围。

## 结论

Stage 0 的八项完成条件全部有可复查证据，验收矩阵无缺口。Stage 0 完成；Stage 1
未开始，也未获得实施授权。任何 Stage 1 开发、真实业务数据写入、网络/QMT 或新的外部
备份操作仍须按任务另行取得明确授权。
