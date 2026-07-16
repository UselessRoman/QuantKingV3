# Stage 0 持久化、任务与日志契约

状态：S0-03 已实现并通过开发 agent、主 agent 和 S0-07 的独立复核；Stage 0 已完成验收。

本文件记录 S0-03 固化的 schema、状态机、事务边界、SQLite 锁参数、并发门和 S0-04 消费边界。发生冲突时仍以仓库根目录 [AGENTS.md](../../AGENTS.md) 的权威顺序为准。

## Schema

初始 Alembic revision 为 `20260715_0001`，只建立通用工程表：

| 表 | 用途 | 关键约束 |
|---|---|---|
| `job_runs` | 通用持久任务及当前状态 | `job_type + idempotency_key` 唯一；状态、进度、行版本有数据库约束 |
| `job_dependencies` | 通用任务上游依赖 | 两端均为 `job_runs` 外键；上游未成功时 Worker 不得领取 |
| `job_events` | 低频任务状态、心跳和进度事件 | SQLite `INTEGER PRIMARY KEY AUTOINCREMENT` 全局单调游标；按 `job_id,event_id` 建索引 |
| `audit_records` | 通用只追加审计 | `UPDATE`、`DELETE` 均由数据库 trigger 阻断 |

`AUTOINCREMENT` 使 SQLite 通过 `sqlite_sequence` 记住历史最高事件 ID；即使任务级联删除使 `job_events` 变空，后续事件也不会复用旧游标。完整 JSON Lines 日志没有 SQLite 表，不逐行写入数据库。ORM 行只位于 `jobs/adapters/persistence`；`JobRun`、`JobEvent` 和 `AuditRecord` 是与 ORM 分离的领域对象。

`JobRun` 持久保存稳定 ID、任务类型、幂等键、输入/配置/版本清单、请求者、创建/开始/结束/UTC 心跳、阶段、进度、Worker、受管理日志标识、产物清单、结构化失败、取消请求/结果、非可取消阶段、行版本和上游任务 ID。

## 状态机

唯一允许的转换为：

```text
PENDING -> RUNNING
PENDING -> CANCELLED
RUNNING -> SUCCEEDED | FAILED | CANCELLED | INTERRUPTED
```

允许 `PENDING -> CANCELLED` 是为了让尚未领取的取消请求立即终止，避免必须先伪造一次 `RUNNING`。所有终态不可再次转换。`FAILED` 和 `INTERRUPTED` 必须保存结构化失败；`RUNNING` 必须保存 Worker ID；终态保存结束时间。状态和对应 `JobEvent` 在同一短事务提交。

进程恢复将遗留 `RUNNING` 转成 `INTERRUPTED`，写入 `WorkerInterrupted` 失败和 `job_interrupted` 事件。协作取消只在 handler 主动调用安全 checkpoint 时生效；显式非可取消上下文会持久化阶段名，并在退出时清除。

## 事务与 Worker 边界

- 每次仓储调用创建新 SQLAlchemy session；成功显式提交，异常回滚并关闭。
- 锁重试的每次尝试都创建新 session，不复用失败事务。
- 重试只包围调用方声明为可安全重放的短事务，并且只识别 SQLite `BUSY`/`LOCKED`。
- 其他 `OperationalError` 原异常抛出；达到上限后抛出稳定 `PersistenceBusyError`，不得记录后返回成功。
- 领取状态与领取事件原子提交后，Worker 才调用显式注册的 handler；handler 和长计算不在数据库事务中。
- 相同 `job_type + idempotency_key` 使用数据库唯一约束和冲突忽略；只有输入/配置/版本清单、请求者、日志标识和上游集合完全相同时才原子返回同一任务。任一内容不一致均抛出 `IdempotencyConflictError`，不重复事件或副作用。
- 状态转换以当前状态和 `row_version` 做 compare-and-swap；两个 Worker 竞争同一 `PENDING` 任务时只有一个能够提交 `RUNNING` 和领取事件。
- 心跳、重复进度按下述最小间隔合并；完整日志不进入 SQLite。

Stage 0 不提供既有数据库升级入口。`initialize_database` 只初始化此前不存在的新文件：调用方必须给出绝对路径，父目录必须已经存在且整条路径不能经过 symbolic link/junction，目标即使只是一个零字节文件也会在打开 SQLite 连接或执行 Alembic 前被拒绝。函数以排他创建原子保留目标，因此并发出现的同名文件也不会被迁移。pytest、并发门和 Web E2E 入口均只在各自新建的临时目录中调用该函数。

Alembic 中途失败时，新建的失败数据库作为诊断证据保留；它不会被删除或回退成可用路径，同一路径的下一次初始化会因目标已存在而拒绝。该失败产物不是已初始化数据库，后续尝试必须使用新路径。

未来对任何既有库实施正式 schema 迁移前，必须先取得“已完成且验证通过”的 S0-05 一致性快照凭证，并在隔离临时副本上通过迁移验证后，才允许打开原库执行迁移。凭证类型、正式迁移入口和既有库变更流程均未在 Stage 0 实现，也不得以 `initialize_database` 绕过。

## 已固化 SQLite 参数

参数写入 [默认配置](../../configs/default.toml)：

| 参数 | 值 | 语义 |
|---|---:|---|
| `busy_timeout_ms` | 250 | 每个连接等待 SQLite 锁的最长单次时间 |
| `max_attempts` | 7 | 包含首次尝试的总尝试次数 |
| `retry_base_delay_ms` | 10 | 指数退避基数 |
| `retry_cap_delay_ms` | 160 | 单次退避上限 |
| `retry_jitter_ratio` | 0.25 | 退避时间的正负抖动比例 |
| `heartbeat_min_interval_ms` | 1000 | 同一任务心跳最小持久化间隔 |
| `progress_min_interval_ms` | 250 | 完全重复进度事件的最小持久化间隔 |

配置验证同时强制有限上界：`max_attempts` 为 `1..20`，退避基数和上限为 `0..5000 ms` 且上限不小于基数，心跳/进度间隔为 `1..60000 ms`。环境变量覆盖不能把有界重试变成实质无限等待。

连接建立时逐连接设置 `foreign_keys=ON`、`journal_mode=WAL` 和 `busy_timeout=250`。显式持写锁测试使用 3 次尝试、5～10 ms 退避和零抖动，证明在 3 秒内抛出 `PersistenceBusyError`。

## 固定多进程门

验收测试必须使用 Windows `multiprocessing.get_context("spawn")`：4 个独立进程，每进程 25 个通用 Worker 活动短事务，共 100 次；单 pytest 设 30 秒硬上限。每次事务原子更新任务行版本/心跳/进度，追加一个 `JobEvent` 和一个通用 `AuditRecord`。

2026-07-15 当前开发环境实测：

```text
processes=4
transactions=100
elapsed_seconds=1.797
pytest_result=1 passed in 2.27s
```

验证包括：4 个子进程退出码均为 0、结果队列无错误、任务保持 `RUNNING`、行版本精确增加 100、100 个活动事件、100 条审计、事件摘要不重复且全局游标严格递增。S0-07 已独立复跑该门，结果为 `2 passed in 3.38s`。

## JSON Lines

`JsonLinesFormatter` 和 `JsonLinesHandler` 接受标准库及第三方 `LogRecord`。每行是一个完整 JSON 对象，必含 `event`、UTC `timestamp`、`level`、`logger`、`message`；可带任务、关联和实体字段。异常保存类型、脱敏消息、stack 和异常链。

字段名按大小写及连字符、下划线、空格归一化，递归掩码 password、token、secret、authorization、API key、cookie 和完整账户；自由文本中的常见 `key=value`/`key: value` 形式同样脱敏。Handler 必须同时接收已经存在的绝对受管理日志根和相对 `log_id`，拒绝绝对标识、`..`、根目录本身、非 JSONL 后缀及 symlink/junction 越界；默认测试仅使用 `tmp_path`。

Worker 会把原 handler 异常重新抛给进程监督者，但 SQLite `failure_json` 只保存异常类型和固定安全消息，不复制可能包含 token、password、账号或路径的 `str(error)`。原始堆栈只能经过上述脱敏 Formatter 写入受管理 JSONL。

## S0-04 端口契约

S0-04 只能依赖应用端口，不得直接访问 SQLAlchemy、SQLite 或日志文件路径：

- 任务快照由 `JobService.get_job(job_id)` 提供。
- 持久事件由 `JobService.list_events(after_event_id, limit)` 提供；`after_event_id` 非负，`limit` 为 `1..1000`，结果严格按 `event_id` 升序，边界后返回空列表。
- SSE 使用该单调游标传输，不能成为 Worker 内存总线；断线重连继续查询 `event_id > cursor`。
- `JobRun.log_id` 是受管理日志标识，不是可由 API 接受的任意本地路径。S0-04 的日志尾随适配器必须在受管理日志根内解析该标识，并以有界字节偏移读取。
- Web schema、HTTP 错误和 SSE 映射属于 S0-04；本任务没有实现 FastAPI、路由、SSE 或前端。

## 未实现边界

- 没有业务任务类型、调度器、数据抓取、策略账、交易、研究或回测逻辑。
- 没有生产迁移备份、本地/F 盘恢复或保留清理；这些属于 S0-05。
- 没有 FastAPI/SSE/React/Playwright；这些属于 S0-04。
- 没有访问网络、QMT、`E:\QuantKingV3Data`、`F:\QuantKingV3Backups` 或 `E:\QuantKing`。
