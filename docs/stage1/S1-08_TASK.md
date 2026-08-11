# S1-08 任务卡：首个正式日线版本与 Stage 1 最终验收

## 状态与授权边界

状态：任务设计完成，未授权执行。所属 Stage：Stage 1。

本任务同时包含少量生产入口收口、一次有界 AKShare 真实运行、写入
`E:\QuantKingV3Data`、真实版本复验和 Stage 1 最终文档验收。开始任何网络调用或正式数据
写入前，必须取得用户对本卡“固定运行范围”的明确授权。授权不覆盖失败重试、扩大证券/日期、
访问 `E:\QuantKing`、访问 F 盘、QMT、券商 SDK、S1-09、Stage 2 或其他阶段。

## 当前基线

- K1 提交：`f98f1d9`；K2 提交：`0ebf84b`，均已独立主验收。
- AKShare 锁定为 `1.18.83`；A=东财、B=腾讯、C=新浪。
- 数据合同和仲裁规则见 [当前合同](CONTRACTS.md)。
- 2026-08-11 只读盘点时，正式数据根仅有：
  - `acceptance/`：86 files、约 0.20 MiB，两组 Stage 0 恢复验收；
  - `validation/s1-07b/`：231 files、约 3.24 MiB，已退役路线的完整/失败审计证据；
  - 尚无 `metadata/`、`raw/`、`artifacts/` 或 `warehouse/` 正式业务目录。
- 上述数据根内容体积很小，且包含恢复验收和失败证据；本任务不得删除、覆盖或搬移。

代码根清理不是阻断项：`.mypy_cache`、Python bytecode、pytest/Ruff cache、`frontend/dist`、
`frontend/test-results` 和 `tmp` 合计约 43 MiB，可在需要时重建；`.venv` 约 908 MiB、
`frontend/node_modules` 约 112 MiB，是当前锁定环境。S1-08 默认全部保留，不以清缓存冒充
数据清理，也不在正式运行前删除可用于诊断的失败产物。

## 固定首发范围

| 项 | 固定值 |
|---|---|
| dataset/schema | `daily_bar_unadjusted` / `daily_bar_unadjusted.v1` |
| symbols | `000001.SZ`, `600000.SH`（排序后冻结） |
| target window | `2026-06-11..2026-08-09`，首尾均包含 |
| upstream lookback | `2026-05-27..2026-08-09`，15 个自然日 |
| provider/upstream | AKSHARE / EASTMONEY、TENCENT，异常时 SINA |
| AKShare | `1.18.83` |
| publish intent | `PUBLISH_AFTER_QUALITY` |
| data root | `E:\QuantKingV3Data` |
| application retry | 0 |

目标窗口故意复用 K1 已验证的两只沪深证券和 60 个自然日，既能覆盖两个交易所和腾讯
`000xxx.SZ` 特殊成交量单位，又不把首个 Stage 1 版本冒充全市场研究数据。

## 网络预算

生产 chunk 固定为每次 1 个证券、最多 31 个自然日。正常 A/B 路径各为 4 次 AKShare
函数调用，共 8 次；C 只在异常时调用，最多再 4 次。必须在代码中加入先计划后执行的预算
守卫：

- 总 AKShare endpoint invocation 最多 12 次；
- C 的全部受影响 span 先计算并分块，若预计超过剩余 4 次预算，则在首次 C 调用前以
  `NETWORK_BUDGET_EXCEEDED` 失败；
- 应用层零自动重试，失败后不得在同一授权下重跑；
- 锁定源码下，东财每次至多 1 个 HTTP 请求，腾讯至多 3 个，新浪至多 2 个，因此整个
  任务预计原始 HTTP 上限为 24；package contract 变化必须在联网前阻断。

预算统计、实际 endpoint 调用和实际 upstream 请求数必须写入 Job 日志和最终验收报告。

## 实现任务

### 1. 让生产范围显式可执行

- 在严格配置合同加入 `[eod]`，默认且唯一首发 scope 为上述两个 symbol；拒绝空、重复、
  非规范代码、非 K1 单位白名单代码和未知字段。
- `EodExecutionConfig.production` 从已验证设置构建 scope；冻结 config manifest 和输入指纹
  必须包含精确 scope，不能从数据库或“latest”动态选择。
- CLI 的 `eod-enqueue` 增加显式 `--start-date/--end-date`，保留单日 `--trade-date` 兼容；
  两种形式互斥。Web/API 继续调用同一个 `EodCommandService`。
- runtime import、CLI `--help` 和 enqueue 阶段不得访问网络或创建 Raw。

### 2. 固化有界网络与预检

- 在 source port/ingestion 边界实现 endpoint invocation 预算，而不是只在脚本中计数。
- 新增只读 dry-run 计划，输出 scope、target/upstream window、A/B chunk、最大 C chunk、
  版本/规则和目标 data root；dry-run 不创建数据库、不写目录、不导入调用 AKShare。
- 联网前核实数据根仍无 `metadata/raw/artifacts/warehouse`；若出现未知业务数据则停止，不
  自动迁移、覆盖或清理。
- 首次创建 `metadata/quantking.db` 时升级到唯一 head `20260810_0014`；迁移失败保留现场，
  不继续采集。

### 3. 执行唯一正式 Job

1. 保存 dry-run 输出和数据根预检清单。
2. 使用稳定 idempotency key `s1-08-first-release-20260611-20260809-v1` enqueue。
3. 由正式 Worker 执行一次；不得用测试 fake 或直接调用适配器绕过 Job。
4. A/B 正常一致时 C 必须为 0；如触发 C，只允许任务实际计算出的受影响范围且受预算约束。
5. 成功时只发布一个最终 `DatasetVersion`；失败时保留 Raw、日志和失败候选，不修改为成功。

### 4. 独立复验

新增只读 `scripts/verify_s1_08_release.py --version-id <explicit-id>`。脚本只接受当前 data root
中的显式 ID，不接受 `latest` 或任意路径，至少复验：

- 数据库 revision、Job 终态、DatasetVersion 为 `PUBLISHED`；
- manifest/`COMPLETE`、内容 SHA、文件集合和数据库交叉绑定；
- scope、target window、实际 coverage、row count、唯一键和固定排序；
- OHLC、`pre_close`、非负量额、单位和最终质量 `PASSED`；
- 全部实际 Raw batch、provider/upstream/endpoint、15 日 lookback 和 Job 绑定；
- DifferenceReport、ResolutionRun、默认决定、例外/冲突、C 实际范围；
- 无冲突行进入发布版本，无 BUILDING/VALIDATED 悬挂候选；
- DataCenter 对显式 version 的版本、批次、质量、差异、resolution 和分页预览可读。

验收脚本输出规范 JSON 到控制台；只有经用户授权后才可把最终报告写入正式 data root。

### 5. 幂等与 Web 验收

- 同一 idempotency key 重复 enqueue：返回原 Job，`created=false`，零网络、零新版本。
- 使用新 idempotency key 但完全相同冻结输入创建新 Job：Worker 严格复验并复用同一个
  PUBLISHED version，零 upstream 调用。
- DataCenter Web 必须通过显式版本展示 coverage、来源批次、质量、DifferenceReport、
  resolution 摘要/例外/冲突和预览；浏览器不加载完整原始表或绝对路径。

## 验收与交付

功能验收必须同时满足：

- 唯一正式 Job 成功，或严格复用 Job 成功；至少一个明确范围版本处于 `PUBLISHED`。
- 两只证券的实际交易日数据全部位于目标窗口；不要求周末产生行，也不预设 row count。
- 每个发布行至少有两个 upstream 零容差一致；无共识即整项不通过。
- Raw、质量、Difference、Resolution、Warehouse 和 Job 血缘完整可重建。
- 两种幂等路径均为零网络且不产生新发布版本。
- DataCenter API/Web 对真实版本可查询，失败和缺口明确展示。
- 北交所、退市、复权、公司行动、历史 ST/停牌和动态股票池继续列为未覆盖，禁止宣称
  Stage 2 研究就绪。

代码变更后先完成全部离线质量门，再申请真实授权；真实运行结束后再重跑不访问网络的
完整质量门。最终创建 `docs/stage1/S1-08_ACCEPTANCE.md`，更新本目录 README、
`docs/ROADMAP.md` 和根 README。只有主 agent 独立复验全部通过，才可声明 Stage 1 完成。

## 固定质量门

- `uv lock --check` 与 frozen sync；
- Ruff lint/format、mypy、架构边界；
- 全部离线 pytest（排除所有 real marker）；
- frontend typecheck、unit、production build、关键 Playwright；
- Markdown links、`git diff --check`；
- S1-08 dry-run/预算/真实 verifier 的正负合同；
- 历史迁移可从空库升级到 0014，历史迁移字节不改。

## 建议授权原文

> 授权执行 S1-08：允许在 `E:\QuantKingV3Data` 保留既有 acceptance/validation 内容并创建
> metadata、Raw、artifact、日志和 Warehouse；允许仅对 `000001.SZ`、`600000.SH` 执行
> `2026-06-11..2026-08-09` 目标窗口及固定 15 日 lookback 的 AKShare 东财/腾讯/异常时新浪
> 日线调用；AKShare endpoint invocation 总计最多 12 次、锁定源码预计 HTTP 总计最多
> 24 次、应用层零重试。失败后停止并报告，不自动重跑或扩大范围；不访问旧项目或 F 盘，
> 不启动 QMT，不连接券商，不开始 Stage 2。

## 明确不在范围

- 清理或迁移既有 Stage 0/S1-07B 验收证据；
- 全市场、全历史、北交所、退市证券或其他 symbol；
- 复权、公司行动、交易状态、动态股票池、指数成分；
- F 盘备份或恢复演练；
- 调度器、自动重试、人工 override、券商接口；
- Stage 2 及以后工作。
