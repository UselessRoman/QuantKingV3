# S1-08R2 独立验收记录

本记录是不可变验收事实记录。验收通过后只追加新记录，不原地修改本文件。

## 1. 验收日期与角色

- 验收日期：2026-08-17。
- 验收角色：S1-08R2 独立只读验收 agent。该验收上下文未参与 R2 实现、测试编写或
  dry-run 演示；结论以仓库代码、工作树 diff、定向测试与实际门禁输出为依据，不复述
  实施记录。
- 验收范围：S1-08R2 全部工作（R2A 存储幂等与恢复、R2B manifest 驱动的范围与预算、
  R2C 只读 verifier/DataCenter/Web、合成 E2E）。

## 2. 基线与变更边界

- 基线提交：`48d8180`（`feat(data): complete S1-07K4 tri-source closeout`），已确认在
  历史中（验收时 HEAD 即该提交）；R2 全部变更为其上的未提交工作树修改。
- `git status --short` 的全部修改/新增逐项核对为任务卡声明的 R2 变更集（18 个生产/
  脚本/前端文件修改、2 个新增生产模块 `publish_lock.py`/`release_manifest.py`、
  6 个新增测试文件与 `docs/stage1/S1-08R2_IMPLEMENTATION.md`）。
- 用户自己的未提交 Stage 2 规划改动（`docs/stage2/` 目录与八份根/领域文档中的
  S2-01～S2-06 段落）不属于 R2，未审计为 R2 缺陷、未修改、未回退。
- 本验收未执行任何 Git 元数据写入。

## 3. 攻击性审计结论（13/13 无阻断）

1. **固定两证券残留——通过**。`S108_FIRST_RELEASE_SYMBOLS` 已从
   `common/config.py` 删除，`eod_symbols` 改为 1..64 上限的规范交易所/证券代码通用
   校验；`release.py` 已收缩为纯 K4C 历史 probe 规划器（`build_s108_dry_run` 删除），
   `EodExecutionConfig.production_k4` 在 `src/` 内仅被 `build_k4c_representative_dry_run`
   调用、后者无生产调用方；`release_verifier.py` 的 `S108_FIRST_RELEASE_SYMBOLS`
   （模块内重新定义）引用全部位于 `verify_s108_release` 历史辅助链（L334-1316），新
   `verify_manifest_release`（L1348 起）scope 全部来自 manifest。`k4d_pilot.py` 六证券
   为 ADR-027 拒绝后的无活跃执行入口；其余 `000001.SZ` 命中为 Stage 0 Qlib 探针与
   canonical 代码格式注释。
2. **manifest 篡改——通过**。`ReleaseManifest.from_dict` 在 `__post_init__` 全量复验
   （symbols_sha256 重算 `release_manifest.py` L288-292、A/B/C chunk 清单与确定性
   inventory 重算比对 L340-352、budgets 对照 `RELEASE_SAFETY_CAPS` L363-386、
   coverage segments/gaps/exemptions 语义 L123-156）后再做独立 canonical-form 整体
   比对（L564-565）。篡改用例真实 fail closed：重复/乱序证券、错误 SHA、缺字段、
   超预算、篡改 chunk、复活 legacy（`test_release_manifest_contract.py` L118-154）；
   CLI 换证券集 → `DRY_RUN_FAILED`（`test_eod_cli.py`
   `test_dry_run_subprocess_rejects_a_tampered_manifest`）；E2E 内对已发布版本用篡改
   manifest 复验被拒（`test_s1_08r2_e2e.py` L400-421）。
3. **预算绕过——通过**。`RELEASE_SAFETY_CAPS` 在构建（`__post_init__`）、解析
   （`from_dict`→`__post_init__`）、冻结核解析（`frozen.py` R2 分支独立强制端点总量
   =角色和、HTTP=B×3+C×2、协议=A×3、超上限拒绝、D 角色恒 0、legacy SHA 恒 null）
   三端强制；`EodExecutionConfig.from_release_manifest` 精确绑定 manifest 预算并随
   冻结核下发 SHA。worker `K4EndpointBudget` 逐角色限额取自冻结核（`eod_worker.py`
   L165-170），日历不完整时 preflight 零调用有直接断言（`test_eod_worker_k4.py`
   L1122-1124 `bao_gateway.calls == []`）；E2E 断言 fake provider A=8、B=8、C=0 且
   预算守恒 24/24。
4. **SQLite 锁竞争——通过**。`execute_with_retry`（`jobs/adapters/persistence/sqlite.py`
   L895-927）与数据 UOW 共享同一实现：`is_sqlite_lock_error` 按 sqlite_errorcode 低位
   + 消息白名单只重放 BUSY/LOCKED，约束/未知错误立即抛出，超限抛
   `PersistenceBusyError`；sleeper/jitter 可注入。回归覆盖：注入退避序列
   （0.01/0.02）、超限失败且错误脱敏、约束错误零重试、真实跨连接 BEGIN IMMEDIATE
   竞争（释放后成功）、持续持锁 fail closed（`test_data_uow_retry.py` 5 用例）。
5. **空版本——通过**。合法空版本重复发布收敛同一目录、同一 manifest 字节、
   mtime/size 不变、目录唯一；冲突重放（同 version_id 不同输入）抛
   `IdempotencyConflictError`；空版本 manifest 仍绑定 code/rule 版本与零计数 coverage
   （`test_s1_08r2_publish_reliability.py` L131-172；`canonical_warehouse.py`
   L237-239、L435-563 全凭据比对含 content_sha256）。
6. **发布竞态——通过**。`VersionPublishLock.acquire` 用 `os.open(O_CREAT|O_EXCL)`
   原子创建（`publish_lock.py` L78），活锁 `PUBLISH_LOCK_HELD` fail closed、有界等待
   （默认 30s、注入 sleeper）；Windows `multiprocessing spawn` 双进程同 version_id
   恰一个 owner、另一个幂等收敛、最终目录通过 verifier、无 `.incomplete` 残留
   （`test_s1_08r2_publish_reliability.py` L253-289）；`.incomplete
   mkdir(exist_ok=False)` 与进程内 RLock 构成数据库唯一键之外的双重守卫；stale 锁
   保留式 rename 回收（L292-315）。
7. **硬中断——通过**。中断 Job 标记 `INTERRUPTED`、重试新 Job 携带
   `retry_of_job_id`（冻结核字段与 command service 校验保留）；
   `recover_stale_incomplete` 三分类（staging-interrupted / promotion-interrupted /
   orphaned-after-publish，`canonical_warehouse.py` L296-306）保留式改名证据不删除、
   释放幂等键后可重跑；staging-interrupted 主路径有完整恢复+重发布测试
   （`test_s1_08r2_publish_reliability.py` L321-344）。
8. **内部 coverage gap——通过**。`PublishedEodBaseResolver` 逐证券连续段 run-length
   （`eod_base.py` L77-100，内部缺口保留为独立窗口，不再全局 min/max）；
   `SecurityCoveragePlan` 语义：gaps 必须在 span 内且不与 segments 重叠、豁免原因限定
   SUSPENSION/PRE_LISTING/DELISTED/NO_TRADING_SESSION、乱序/越界拒绝；planner 只重取
   缺口窗口、逐证券独立、lookback 按证券重开（`test_incremental_coverage_r2.py`
   8 用例）；E2E 停牌证券 57 行两 segments 断言。
9. **verifier 写副作用——通过**。`_readonly_database` 以 `mode=ro&immutable=1` URI +
   `PRAGMA query_only=ON` 打开、存在 `-wal/-shm` sidecar 即拒绝
   （`release_verifier.py` L126-148）；E2E 在验证前后对数据根全文件集合做
   size/mtime_ns/SHA-256 快照比对一致，且验证后无 sidecar 残留
   （`test_s1_08r2_e2e.py` L376-386）；`_release_version_id` 拒绝 `latest`、路径分隔符
   与非 canonical ID（L109-122）。
10. **路径/SQL 注入——通过**。verifier 与 DataCenter 两个新读侧入口均经
    `parse_entity_id` + namespace + 规范形式校验；SQL 全部参数化（`?` 占位），
    ORM 查询无字符串拼接；E2E 对 `latest`/`../escape`/非 canonical/空 ID 的拒绝断言
    （L424-429）。
11. **数据泄露——通过**。`get_version_source_projection`/
    `get_version_security_coverage` 只返回 role/计数/状态/时间段，无绝对路径、
    provider payload 或异常栈；E2E 断言 lineage 序列化不含 data_root 与
    `provider_payload`（L288-290）；失败 evidence 脱敏测试断言无 `E:\private`、无
    token、无 Traceback、不写 warehouse、不可变（`test_failure_evidence_r2.py`）；
    warehouse `failure.json` 构造剥离绝对路径并只用稳定错误类别
    （`canonical_warehouse.py` L920-975）。
12. **XTQuant 重新进入发布链——通过**。manifest 绑定 `LegacyDisposition`
    （REJECTED + ADR-027 + inventory/diagnosis 双 SHA + included/published 计数恒 0 +
    `import_manifest_sha256` 恒 null，构造器强制 `release_manifest.py` L225-243）；
    篡改复活 legacy 在 from_dict 失败；`_verify_manifest_release` 对 lineage 中
    `XTQUANT_LEGACY` provider/upstream 直接 fail closed；R2 执行配置与冻结核解析均
    强制 legacy SHA 为 null；DataCenter 源投影把 `XTQUANT_LEGACY` 展示为 ADR-027
    REJECTED/排除（`center.py` legacy_disposition 块）。
13. **合成 E2E——通过**。`test_s1_08r2_e2e.py` 单链覆盖：4 证券（>2）、每角色 8 chunk
    （两倍首发固定 4）、300750.SZ 三日停牌内部缺口（57 行双段）、预算守恒
    （planned 24 = A8+B8+Cmax8，actual A=8/B=8/C=0）、来源投影 `/sources`、
    coverage 分页 2+2 cursor、幂等重放（created=False、同 Job、版本数仍 1）、
    显式版本只读 verifier 快照不变。全程临时根与进程内 fake provider，网络 socket
    守卫只放行 loopback。

## 4. 定向测试实际结果（本验收独立执行）

- `tests/data/adapters/test_data_uow_retry.py` +
  `tests/data/adapters/test_failure_evidence_r2.py` +
  `tests/data/eod/test_release_manifest_contract.py` +
  `tests/data/test_incremental_coverage_r2.py`：**19 passed**（4.05s）。
- `tests/integration/data/test_s1_08r2_e2e.py` +
  `tests/integration/data/test_s1_08r2_publish_reliability.py`：**8 passed, 1 warning**
  （12.03s；含 Windows spawn 双进程单 owner 用例）。
- `tests/web/test_data_center_contract.py` + `tests/docs/test_stage1_status.py`：
  **47 passed, 1 warning**（2.04s）。
- `tests/common/test_config.py` + `tests/cli/test_eod_cli.py` +
  `tests/data/eod/test_command_service.py`：**43 passed**（11.83s）。

## 5. 完整门禁实际结果（本验收在最终代码状态复跑）

`scripts\quality.ps1 -UvCacheDirectory .uv-cache` 逐步结果：

- `uv lock --check --offline`：223 packages 解析通过。
- `uv sync --frozen --all-groups --offline`：217 packages 检查通过。
- Ruff lint：All checks passed；Ruff format check：277 files already formatted。
- mypy `src/quantking`：Success，132 source files 零问题。
- 门禁内全量离线 pytest（同 marker 选择）：完成且无失败（脚本顺序执行到达后续步骤）。
- 架构边界检查、Markdown 链接检查：通过（脚本顺序到达后续步骤）。
- 前端 npm clean install（offline）、typecheck、unit tests、production build：通过
  （脚本顺序到达 e2e 步骤）。
- Playwright E2E：**1 passed（8.2s）**，Web server 正常启停。
- `git diff --check`：quality.ps1 内对该 git 进程的直接调用被 TRAE 执行沙箱以无关
  系统后台文件活动（OneAuth/TokenBroker/NVIDIA DXCache/Recent）误判拦截，进程非零
  退出——与 K4 轮同类的环境限制，不是产品测试失败；以工作区规定的只读 Git 包装
  `scripts\git-read.ps1 diff --check` 执行相同检查，**退出码 0、零 whitespace 问题**。
- 同参数独立复跑全量离线 pytest
  （`-m "not real_external and not real_akshare and not real_k3_sources and not
  real_legacy_data and not real_backup" -q -p no:cacheprovider`）：
  **1309 passed、6 skipped、3 deselected、4 warnings（280.69s，退出码 0）**。
  未使用任何 real marker，未运行 compatibility real 测试。

## 6. 外部访问声明

本次验收为纯离线只读验收：

- 真实网络调用：0（所有测试使用合成数据、fake provider 与临时目录）。
- 旧项目根 `E:\QuantKing` 访问：0。
- 运行数据根 `E:\QuantKingV3Data` 写入：0。
- F 盘数据/备份根访问：0。
- Git commit/push/分支修改：0；历史 authorization_id/marker 重放：0。
- 未执行正式 EOD Job；未产生新的真实 `PUBLISHED DatasetVersion`。

## 7. 已知非阻断观察

1. `recover_stale_incomplete` 的 `promotion-interrupted` 与 `orphaned-after-publish`
   两个分类分支没有专项合成测试（主路径 `staging-interrupted` 有完整恢复+重发布
   回归）；分类逻辑为简单存在性判定且与已测路径共用保留式改名。建议 R3 前补齐。
2. `verify_manifest_release` 对 `XTQUANT_LEGACY` batch 进入 lineage 的拒绝分支没有
   专项构造测试（manifest 端有篡改复活 legacy 拒绝测试，E2E 断言 REJECTED 投影）。
   建议以合成 lineage 构造该分支的直接断言。
3. `get_version_security_coverage` 通过有界列投影（code/trade_date）全量加载后内存
   分页；对 Stage 1 ≤64 证券的 manifest 上限有界，不构成泄露或无界查询，但超大版本
   的内存占用可留待后续评审。
4. 主开发实施记录自述全量 pytest "1310 passed、8 skipped"，与本验收同参数独立复跑
   的 1309/6/3 存在计数差异（推测为运行参数或计数口径差异）；验收记录以本验收独立
   复跑数字为准，两者均无失败。
5. TRAE 沙箱对 quality.ps1 内 git 直连进程的拦截为环境限制（K4 轮同类），等价补跑
   通过；不记为产品测试失败。

## 8. 能力验证引用（非授权）

主开发的零网络 dry-run 演示（合成 4 证券 manifest，产物仅写临时目录）：manifest
SHA-256 `e7b9ca1c869dded7c12602f14067d1125423a55293dc2a7a392b4693098d3b9e`、载荷
SHA-256 `f3786ef74aef7939e0807a6da19c78655cb40e62af1cac0ac9f3521f24ed3230`，
`endpoint_total=24`、`requires_new_real_authorization=true`、`network_invocations=0`、
`writes_data_root=false`。本验收引用其作为 R2B dry-run 合同的能力验证；它不是 R3
的授权，也不进入 `E:\QuantKingV3Data`。

## 9. 最终结论

**S1-08R2 独立验收通过。**

R2 期间零真实网络、零旧根读取、零正式数据根写入、零 F 盘访问；尚未产生新的真实
`PUBLISHED DatasetVersion`。下一步是 S1-08R3（冻结发布 manifest、唯一正式 Job、
独立只读复验与双幂等、Stage 1 最终验收），其网络、正式数据根写入及任何失败重跑均
未授权，须由单独任务与新授权启动。Stage 1 尚未完成，Stage 2 未开始。
