# S1-08R2 实施记录：发布引擎离线收口

状态：纯离线实现完成。本轮零真实网络、零 `E:\QuantKing` 访问、零 `E:\QuantKingV3Data`
写入、零 F 盘访问；未重放任何历史 authorization_id/marker；未执行正式 EOD Job 或真实
PUBLISHED 版本；未开始 S1-08R3 或 Stage 2。独立验收记录 `S1-08R2_ACCEPTANCE.md` 由独立
验收 agent 在通过后创建。

## 冻结的 R2 合同

| 项 | 值 |
|---|---|
| 发布 manifest | `s1-08r2-release-manifest.v1`（`data/eod/release_manifest.py`，规范 JSON、可计算 SHA、`from_dict` 独立 fail-closed 复验） |
| dry-run | `s1-08r2-dry-run.v1`（`build_zero_network_dry_run` + CLI `eod-dry-run --manifest …`） |
| 执行配置 | `eod-execution-config.r2.v5`（R2 schema；历史 v2/v3/v4 字节不变） |
| 代码版本 | `s1-08r2.v1`（`runtime.EOD_CODE_VERSION`） |
| 独立安全硬上限 | `RELEASE_SAFETY_CAPS`：证券 64、每角色 chunk 512、每角色端点 512、端点总量 1536、AKShare HTTP 2560、BaoStock 协议消息 1536、Raw 批次 1536、DifferenceReport 1536、Resolution 8、Quality 8、chunk 31 天、lookback 15 天。manifest 声明值在构建与解析两端都对上限校验，不能自行抬高 |
| ADR-027 处置 | manifest 绑定 `LegacyDisposition`（REJECTED + ADR-027 + inventory evidence SHA `140978ea…` + 第 2 次 pilot 诊断 SHA `b75f27b4…`）；`legacy_import_manifest_sha256` 恒为 null；`XTQUANT_LEGACY` 的 included batch/lineage/published count 恒为 0 |

manifest 绑定：`daily_bar_unadjusted.v1` 唯一 Canonical schema、排序去重证券集与集合
SHA、target/upstream 窗口与 lookback、版本化日历 ID/SHA、逐证券 coverage 连续段/内部
gap/显式豁免（原因限定 SUSPENSION/PRE_LISTING/DELISTED/NO_TRADING_SESSION）、A/B/C 角色
路由/adapter/单位/比较/仲裁/质量/代码版本、确定性 A/B planned 与 C planned/maximum
chunk 计划、endpoint/AKShare HTTP/BaoStock protocol 预算、Raw/Difference/Resolution/
Quality 血缘数量上限、`PUBLISH_AFTER_QUALITY` 发布意图、精确 would-write 目录集合与
legacy 处置状态。缺字段、重复/乱序证券、错误 SHA、超预算、超血缘或不一致 coverage 在
构建或解析时 fail closed。

## A. SQLite、幂等、发布并发与恢复

- **统一短事务重试边界**：`jobs/adapters/persistence/sqlite.py` 提取模块级
  `execute_with_retry`/`is_sqlite_lock_error`/`retry_delay_seconds`，数据 UOW
  （`DataSqliteUnitOfWorkFactory.execute`）与 jobs UOW 复用同一实现：仅对可重放的
  `SQLITE_BUSY`/`SQLITE_LOCKED`（按 sqlite_errorcode 低位 + 消息白名单判定）做有界指数
  退避 + 抖动；约束/数据/未知错误立即抛出；超限抛稳定 `PersistenceBusyError`。sleeper 与
  jitter 可注入，测试确定性。回归覆盖真实跨连接锁竞争（持有→释放后成功）与持续持锁
  （超限失败、错误脱敏），见 `tests/data/adapters/test_data_uow_retry.py`。
- **跨进程单发布者**：`warehouse/publish_lock.py` `VersionPublishLock` 以
  `os.open(O_CREAT|O_EXCL)` 原子创建锁文件（PID+UTC 时间戳），同 version_id 的
  Web/Worker/多 Worker 并发只有一个 owner 进入 staging/final 写入；持有方存活时其余方
  有界等待（默认 30s、注入 sleeper），超时 fail closed（`PUBLISH_LOCK_HELD`）后可幂等
  重试收敛；进程退出自动释放，stale 锁可被下一个 owner 以保留原锁文件证据的方式回收。
  Windows 多进程（`multiprocessing` spawn）合成测试证明同一 version_id 两进程并发恰一个
  owner 发布、另一个幂等收敛到同一目录。数据库唯一键不作为唯一证明——文件系统竞态由锁
  与 `.incomplete mkdir(exist_ok=False)` 双重守卫。未引入 Redis/Celery 或新的分布式控制面。
- **合法空版本幂等**：`CanonicalWarehouse` 的 content SHA 与 manifest 对零行表绑定
  `EmptyResultInfo`（显式合法空分类与原因），相同冻结输入重复发布收敛到同一目录、同一
  manifest 字节，不产生第二版本、不重写已发布产物；空版本仍绑定 scope、逐证券
  coverage、规则与来源计划。冲突重放（同 version_id 不同输入）仍抛
  `IdempotencyConflictError`。
- **硬中断恢复合同**：中断 Job 标记 `INTERRUPTED`（重试必须新 Job 且携带
  `retry_of_job_id`，command service 校验父 Job 为 FAILED/INTERRUPTED 的 EOD Job）；
  `recover_interrupted` 幂等只失败该 Job 未发布的 candidate；warehouse 侧
  `recover_stale_incomplete` 显式分类（staging-interrupted / promotion-interrupted /
  orphaned-after-publish）并把 `.incomplete` 保留式改名为
  `<name>.incomplete.interrupted-<utc>`，证据不删除，内容幂等键释放，允许新任务重跑；
  只有完整通过 Raw、Difference、Resolution、Warehouse manifest、COMPLETE 与数据库交叉
  验证的 PUBLISHED 版本才可按输入指纹复用。纯合成恢复测试，未恢复任何真实 pilot/full
  入口（ADR-027）。
- **逐证券真实 coverage**：`PublishedEodBaseResolver` 以连续段 run-length 表达每个证券的
  真实连续区间（内部缺口保留为独立窗口），不再用全局 min/max 冒充连续覆盖；增量计划只
  重取缺口窗口，lookback 按证券独立重开。测试覆盖内部断档、合法豁免、停牌/未上市。
- **失败批次保留**：失败 evidence 保持脱敏 reason chain、受影响行数、质量/隔离计数与
  有界确定排序行级样本；不泄露绝对路径、provider payload、异常栈或完整 DataFrame；
  不污染 Warehouse 或已发布版本（`tests/data/adapters/test_failure_evidence_r2.py`）。

## B. manifest 驱动的范围、预算和血缘

- 生产路径删除对首次两证券/固定日期/固定批次/固定质量与差异数量的依赖：
  `S108_FIRST_RELEASE_SYMBOLS` 从 `common/config.py` 移除，`eod_symbols` 改为
  规范交易所/证券代码 + 上限 64 的通用校验；`release.py` 的
  `build_s108_dry_run`（固定 4+4 chunk 规划器）删除，新发布统一走
  `ReleaseManifest`；`runtime.EOD_CODE_VERSION` 更新为 `s1-08r2.v1`。K4C 历史 probe
  常量（`K4C_REPRESENTATIVE_SYMBOLS`、`build_k4c_representative_dry_run`、
  `production_k4`）保留在隔离历史路径，冻结证据测试字节不变。
- `EodExecutionConfig`：新增 `production`（设置驱动、上限界定的 R2 生产配置）与
  `from_release_manifest`（预算精确绑定 manifest chunk 清单，manifest SHA 随冻结核
  下发）；R2 校验强制 legacy import SHA 为 null、D 角色退役、端点总量=各角色之和且不超
  独立上限、HTTP/协议预算由角色限额推导。`frozen.py` 解析器识别 r2 schema 并同等校验。
  前端“首发 scope 固定为两证券”提示删除，改为 scope 由服务端配置冻结的表述。
- **preflight fail closed**：worker 在首次 provider 调用与正式文件写入前复验冻结核的
  chunk/预算清单（`eod_worker.py` 逐角色 endpoint 预算取自冻结核）；manifest 声明值
  全部受 `RELEASE_SAFETY_CAPS` 独立约束；fake provider 调用计数测试证明 preflight 失败
  时调用数为 0（`test_release_manifest_contract.py`）。
- **零网络 dry-run**：`build_zero_network_dry_run` 从经验证 manifest 生成 R3 完整执行计
  划（输入、chunk、预算、血缘上限、精确 would-write 目录），同输入字节稳定、SHA 稳定、
  规范 JSON 稳定；`requires_new_real_authorization=true`、`network_invocations=0`、
  `writes_data_root=false`，不代表 R3 授权。CLI 篡改 manifest（如换证券集）在 SHA/结构
  复验处失败（`DRY_RUN_FAILED`）。

## C. 只读 verifier、DataCenter 与 Web

- `verify_manifest_release`（`adapters/release_verifier.py`）：只接受显式
  `data_version` ID（拒绝 latest/路径/目录穿越/非 canonical ID），data root 只来自受信
  配置；SQLite 只读复验 Job/Version 输入指纹、warehouse manifest/COMPLETE/规范 JSON/
  逐文件 SHA、实际 Raw/import disposition、Quality/Difference/Resolution 交叉引用、逐证
  券 coverage/内部缺口/豁免/日历/新鲜度、每个发布行的两条实际来源证据（共同缺行不构成
  值共识）、A/B/C planned/maximum/actual 调用与预算守恒、血缘数量守恒与上限、
  `XTQUANT_LEGACY` 拒绝处置与零发布血缘；任一不一致 fail closed，错误脱敏。
  `verify_s108_release`（K4 历史路径）保持不变。
- DataCenter 新增 `get_version_source_projection`（A/B/C 计划/实际 coverage、health、
  freshness、预算；`XTQUANT_LEGACY` 显示为 ADR-027 拒绝/排除状态）与
  `get_version_security_coverage`（逐证券 coverage 段有界分页）；Web 暴露
  `GET /api/data/versions/{id}/sources` 与
  `GET /api/data/versions/{id}/security-coverage`（cursor/limit 有界校验）。契约测试见
  `tests/web/test_data_center_contract.py`。不再因证券数超过 2 或批次数超过首次固定值
  拒绝合法版本。
- **合成 E2E**（`tests/integration/data/test_s1_08r2_e2e.py`）：4 证券
  （000001.SZ/000858.SZ/300750.SZ/600000.SH，含 300750.SZ 3 日停牌内部缺口）×60 日窗口
  → 每角色 8 chunk（两倍于首发固定 4）：synthetic manifest → `EodCommandService`
  enqueue → 合成 worker（fake 三角色 provider）→ quality/resolution/publish → 显式
  版本只读 manifest verifier → DataCenter/API（coverage 分页 2+2、来源投影、预算守恒
  24/24）→ 幂等重放收敛；全程临时根与 fake provider，离线守卫下运行。

## dry-run 演示（零网络、非授权）

- 命令：`uv run python -m quantking.cli.eod eod-dry-run --manifest <合成manifest.json>
  --data-root-label E:\QuantKingV3Data`（经 `scripts/uv.ps1`）。
- 合成 manifest（4 证券、2026-06-11..2026-08-09、15 日 lookback）：manifest SHA-256
  `e7b9ca1c869dded7c12602f14067d1125423a55293dc2a7a392b4693098d3b9e`。
- dry-run 载荷：规范 JSON 5,848 字节、SHA-256
  `f3786ef74aef7939e0807a6da19c78655cb40e62af1cac0ac9f3521f24ed3230`，两次执行字节一致
  （`fc /b` 无差异）；`endpoint_total=24`（A 8 + B 8 + C max 8）、
  `requires_new_real_authorization=true`、`network_invocations=0`、
  `writes_data_root=false`。产物仅写入临时目录，不进入
  `E:\QuantKingV3Data`，也不是 R3 的授权或正式运行。

## 离线门禁证据

`scripts\quality.ps1 -UvCacheDirectory .uv-cache` 逐步结果（2026-08-17，本轮实际运行）：

- 锁文件一致性（223 packages）与 frozen sync（217）通过。
- Ruff lint 与 format check 通过（277 files）；mypy `src/quantking` 通过（132 源文件，
  0 错误）。
- 全量离线 pytest（门禁同 marker 选择）完成 100%，无失败；同参数独立运行
  **1310 passed、8 skipped**（real_external/real_akshare/real_k3_sources/real_legacy_data/
  real_backup 标记测试默认排除，未执行）。
- 架构边界 OK；Markdown 链接 OK。
- 前端 npm ci（--offline，165 packages）、typecheck、unit（**17 passed**）、production
  build（31 modules）通过；Playwright e2e **1 passed (8.1s)**（进程级 TRAE 沙箱在 e2e
  收尾时拦截了无关系统文件访问并回传非零退出码，测试本体通过——与 K4 轮同类的环境
  限制，不记为产品测试失败）。
- `git diff --check`（经 `scripts\git-read.ps1` 同参数等价执行）退出码 0、零空白错误。

完整门禁由独立验收 agent 在最终代码状态上复跑（见验收记录）。

## 外部访问声明

本轮实现与验证期间：真实网络调用 0 次；`E:\QuantKing`（旧根）读取 0 次；
`E:\QuantKingV3Data`（正式数据根）写入 0 次；`F:\QuantKingV3Backups` 访问 0 次；历史
authorization_id/K4C/K4D marker 重放 0 次；所有测试使用合成数据、临时 SQLite 与临时
数据根。R3 的网络、正式根写入与失败重跑均未授权，须由单独任务启动。
