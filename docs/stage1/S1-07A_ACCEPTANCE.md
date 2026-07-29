# S1-07A-R6 finalized seal 最终 TOCTOU 修复交接

状态：S1-07A-R6 修复已提交、等待主验收；24 个单元仍为
BLOCKED_EXTERNAL_VALIDATION；S1-07B/C/D、S1-08 未开始、未获授权。

## 范围与基线

- Stage：Stage 1 / S1-07A-R6。
- R6 基线：`72b56a9a03d5f479acdec039f69fa0b965771cba`。
- 只关闭 incomplete seal 首次深验后到 finalized seal 独占锁之间的最终 TOCTOU；R2～R5
  signer、coverage、quality、sample、authorization、历史隔离和读取边界均未修改。
- 没有迁移数据、发布 `DatasetVersion`、创建 `SourceOverride`、启用 auto-switch 或开始
  S1-08/Stage 2。

## 首次红测

命令：

```powershell
uv run --offline --frozen pytest tests/integration/data/test_s1_07a_r6_finalized_seal.py -q -p no:cacheprovider --tb=no
```

R6 首次结果为 `5 failed, 2 passed`。根因为 `_verify_final(incomplete)` 通过后，incomplete
ZIP 仍可在 `_finalize_seal()` 前被追加未声明/重复 entry 或替换 Canonical/Quality；
finalized 文件只以此前 SHA 进入后续流程，没有在取得独占句柄后重新安全解包和完整深验。
锁前 finalized 截断/替换已被 R5 的 SHA 回滚保护拒绝，因此两项初始即通过。

## 固定样本 v2

- 版本：`s1-07.fixed-sample.v2`
- SHA-256：`03d3d6c1e898b0746b7c5ab3911917a600fa3975f802c1d414fd0b6196c80dfb`
- release 绑定不可变 v1 基础 SHA
  `bc40271a632e0f8b4bae493806bf3d316897857a1102ed01b70f2bb46c632bee`；
  相同 sample version 不能复用另一份规范内容或 SHA。
- completeness/eligibility：`FRAMEWORK_ONLY`。
- 日期范围全部显式为 `2024-01-02`～`2024-01-05`，没有 `today/latest`。

| case_id | dataset | code/exchange | purpose |
|---|---|---|---|
| `adjustment_normal_sh` | adjustment_factor | `600000.SH` | NORMAL |
| `benchmark_sh_composite` | benchmark_index_daily | `000001.SH` | BENCHMARK |
| `benchmark_sz_component` | benchmark_index_daily | `399001.SZ` | BENCHMARK |
| `calendar_bj` | exchange_calendar | BJ | NORMAL |
| `calendar_sh` | exchange_calendar | SH | NORMAL |
| `calendar_sz` | exchange_calendar | SZ | NORMAL |
| `normal_bj_equity` | daily_bar_unadjusted | `430047.BJ` | NORMAL |
| `normal_sh_equity` | daily_bar_unadjusted | `600000.SH` | NORMAL |
| `normal_sz_equity` | daily_bar_unadjusted | `000001.SZ` | NORMAL |
| `security_master_normal_sz` | security_master | `000001.SZ` | NORMAL |
| `trading_status_normal_sh` | trading_status | `600000.SH` | NORMAL |

每个 case 适用于 XTQUANT、BAOSTOCK、AKSHARE、TUSHARE，形成 4 × 6 的待验证矩阵。来源不支持
某单元时必须以真实 `CAPABILITY_UNSUPPORTED` 证据保持阻断。缺少权威事件依据的目的为
`IPO`、`LISTING`、`DELISTING`、`ST`、`SUSPENSION`、`CORPORATE_ACTION`；因此当前样本只可
用于 plan/框架测试，fixed-sample gate 确定性为 false，不能进入 `PRODUCTION_READY`。

## 合同与安全门

### Assessment 持久化来源

唯一公共写入口为
`SqliteSourceAssessmentPersistenceAdapter.persist_from_directory(absolute_assessment_directory)`：

1. 约束绝对 assessment directory 后完成一次受控深验；
2. 以跨进程独占锁原子移走调用方可见目录，并立即用只读 sentinel 保留原路径；原 evidence
   保留在受管 `.assessment-retained` 取证目录；
3. 在保持原始相对路径语义的私有 root 中再次深验，并拒绝未声明顶层项、空目录、symlink、
   junction、reparse point 和非普通文件；
4. 将精确文件集合写成单一 ZIP_STORED incomplete seal，fsync 后安全解包并完成一次完整
   verifier 预检；
5. 以 seal 全文件 SHA 命名并原子完成；如果同 SHA finalized seal 已存在，也不在锁外信任
   文件名或字节比较；
6. finalized seal 获得不共享 WRITE/DELETE 的独占句柄后，重新计算 SHA、重新安全解包并再次
   执行完整 ValidationEvidenceVerifier、精确树检查、signer/attestation summary；结果必须与
   claim 后及 incomplete 预检得到的 `VerifiedAssessmentEvidence`/summary 精确相等；
7. 只有锁内完整复验通过，才在同一连接和事务内启用一次性、短期、精确绑定全部行字段及
   seal 路径/SHA 的 SQLite UDF
   write gate；trigger 消费 gate 后 flush，再核对 seal SHA，commit 前再次核对；
8. commit 完成后才释放 seal、原路径和跨进程锁。竞争者不能在原路径新增嵌套目录，单文件
   seal 也不存在目录项新增窗口；异常回滚且写入稳定脱敏 failure 取证。

通用 `DataRepository` 与 `DataSqliteUnitOfWorkFactory` 已删除全部 assessment 写方法和
`assessment_persistence_key` 参数。公共入口不接受 `VerifiedAssessmentEvidence`、DTO、receipt、
seal 或调用方 key；内部 insert 只存在于专用 adapter。架构边界检查禁止其他源码模块导入私有
assessment DTO/receipt/insert 名称。不存在实际 evidence directory 时，不存在应用级写入路径。

`0012` trigger 调用只安装在专用 adapter 所持数据库连接上的 UDF。gate 只能消费一次，绑定
精确行、`SEALED_VERIFIED_V1`、seal 相对路径及 SHA，并在 10 秒内有效；跨连接、事务结束、
异常回滚、重复或过期调用均返回拒绝。普通 SQLite/SQLAlchemy 连接没有 gate 或只有已 disarm
gate，因此 REAL/PRODUCTION、SYNTHETIC/BLOCKED、无目录及伪造 SHA 的合法外形 INSERT 都失败。

该 UDF gate 的威胁边界是防止普通应用代码、通用 UoW 和未授权 SQL 路径误写或旁路写入；
它不声称能够防御拥有数据库文件完全控制权的本机管理员或恶意进程，因为其可以注册同名
UDF、删除 trigger 或直接修改数据库文件。无论数据库行如何出现，缺少真实 finalized seal、
seal SHA 不符或深验血缘不一致的伪造行仍由 `SealedAssessmentReadVerifier` 失败关闭，绝不显示
为有效 assessment，也不能提升为 `PRODUCTION_READY`。

由于 0010/0011 曾存在旁路，`0012` 将所有既有行逐列原样复制到不可 INSERT/UPDATE/DELETE 的
`source_candidate_assessment_quarantine`，标记 `PRE_R5_UNVERIFIED_WRITE_PATH`，再从活跃表
移除；不静默删除、不改写 REAL/SYNTHETIC，也不参与 DataCenter 最新状态。存在活跃或隔离行时
禁止降级，避免丢失信任凭证；空表支持 `0011 → 0012 → 0011 → 0012`。唯一 Alembic head 为
`20260729_0012`。

通用 repository/UoW 仍只有 assessment 读取能力。活跃读取只考虑 `SEALED_VERIFIED_V1`，
并要求注入 `SealedAssessmentReadVerifier`，逐次核对 seal 文件 SHA、安全解包深验结果、
数据库 assessment、signer 和 attestation 摘要；缺 verifier、缺 seal 或任一不一致均失败关闭。
DataCenter 将该错误稳定映射为读取完整性失败，不展示为有效来源 assessment。

### 持久 authorization claim

`AuthorizationClaimStore` 现在要求受控绝对 `claim_root`，每个 authorization_id 映射到固定
文件名，并以 `O_CREAT | O_EXCL` 单次原子创建。claim 文件绑定 authorization/invocation、
source、dataset、output_root、sample SHA、claimed_at、expires_at 和 `CLAIMED` 状态；不保存
授权签名、密钥、token 或供应商 payload。文件创建成功即永久消费，不因 runner import 或
execute 失败删除。

新 store 实例、独立进程和 runtime 重启都通过同一个不可覆盖 claim 文件识别已消费授权；
并发竞争恰好一个成功，其余稳定返回 `AUTHORIZATION_ALREADY_CONSUMED`。claim 根任一祖先为
symlink/junction/reparse point 时返回 `AUTHORIZATION_CLAIM_STORE_UNSAFE`。S1-07A 正式 CLI
仍为 `AUTHORIZATION_RUNTIME_UNAVAILABLE`。

- writer 信封只保存 run/request/version/时间等不可变事实，不接受或写入调用方
  `evidence_kind`/gate/content SHA/quality verdict。
- verifier 从规范 request manifest 与完整固定 case 集、受管理 Raw verifier、严格 Canonical
  schema/主键/排序/覆盖与统一 content SHA、完整 PASSED QualityRun 及其版本血缘、严格
  DifferenceReport 行证据/汇总/阈值/双方内容血缘，确定性推导五项 gate。
- REAL 身份只由仓库受控 signer key-id/profile allowlist 决定，调用方属性无效。Fake key
  `quantking.fake-runner.v1` 永久映射为 `SYNTHETIC`；S1-07A allowlist 没有任何 REAL signer。
- runner 在所有产物完成后签发 result attestation，绑定 authorization/invocation/session/run、
  source/dataset/request/fingerprint、Raw manifest SHA、Canonical content SHA、Quality
  manifest SHA/QualityRun ID、可选 DifferenceReport ID/SHA、完整文件清单 SHA、八项冻结版本和
  开始/结束时间。签发后替换任一产物都会先因 inventory 或产物血缘不匹配被拒绝。
- coverage 按六类 descriptor 推导：calendar 要求请求范围每个自然日和 exchange；
  daily/factor/status/index 由代码后缀确定交易所，并要求受控 calendar reference 自然日完整且
  每个应开市日都有该代码行；security_master 使用有效区间相交语义。缺证券、错误交易所及
  首/中/尾缺口均失败；没有充分权威 reference 时保持 false。
- quality gate 使用 `default_rule_set()` 的精确 `rule_version` 与
  `for_dataset(dataset)` 目录；结果集合必须无缺少、无额外、无重复，全部适用阻断规则 PASSED，
  并复验 content/schema/run ID、row evidence 和精确文件集合。`FAKE_OFFLINE_CONTRACT`
  不再能通过。
- `run_fingerprint` 完整包含 source、dataset、固定 request/case、sample SHA 和
  adapter/package/code/schema/rule/threshold/sample/config 版本。
- 三次稳定运行要求 fingerprint 相同，且 run_id、invocation_id、session_id、开始/结束时间
  均独立；任何运行失败、gate 失败、凭证漂移或样本不合格均保持阻断。
- writer 使用 `.incomplete` 构建、规范自签名 manifest、逐文件 SHA/大小/行数、`COMPLETE`
  和原子 rename；相同 ID/相同字节为 no-op，不同内容冲突。
- verifier 拒绝 manifest、COMPLETE、文件集合、文件 SHA、路径和 lineage 篡改，以及
  symlink/junction/reparse point 和路径逃逸。
- assessment 只能由目录权威 SQLite adapter 保存；锁内复验重新推导状态并核对统一
  fingerprint、证据路径/SHA、样本资格及 signer/attestation 摘要。Python seal、私有 DTO、
  receipt 与调用方 key 均不是可信入口。
- `plan`、`--help`、import、`status`、`verify` 不导入供应商 SDK。`execute` 的固定
  acknowledgement 只作交互确认；必须先原子 claim 逐次授权的
  authorization_id/source/dataset/output_root/sample SHA/有效期，并绑定唯一 invocation_id，
  随后才允许 import runner。串行重复和并发竞争稳定只允许一个 claim；授权在 runner 失败时
  也已消费。S1-07A CLI 没有受控授权 runtime，稳定返回
  `AUTHORIZATION_RUNTIME_UNAVAILABLE`；受控注入测试首次到达 `RUNNER_UNAVAILABLE`，第二次为
  `AUTHORIZATION_ALREADY_CONSUMED`，均为退出码 2 且不泄漏 traceback。

## 离线 CLI

```powershell
python scripts/run_source_validation.py plan --source XTQUANT --dataset exchange_calendar --output-root <authorized-absolute-root>
python scripts/run_source_validation.py execute --source XTQUANT --dataset exchange_calendar --output-root <authorized-absolute-root> --ack I_ACKNOWLEDGE_AUTHORIZED_REAL_SOURCE_VALIDATION --authorization-file <one-time-authorization.json>
python scripts/run_source_validation.py verify --validation-root <authorized-absolute-root> --directory <evidence-directory>
python scripts/run_source_validation.py status --validation-root <authorized-absolute-root>
```

`plan` 只展示固定 case、范围、字段、SDK、未来 execute 的网络/QMT访问、输出目录、副作用和完整
无密命令。失败时序为 acknowledgement → 授权 runtime → scope/有效期 → 原子 claim/consume →
runner import → session/evidence write；任一授权不匹配均在 import、会话创建和证据写入前失败。
S1-07A 不包含 provider runner 或 CLI REAL signer。

## 外部访问与副作用

- 网络：无。
- QMT/XTQuant：未连接、未加载。
- BaoStock/AKShare/Tushare：未安装、未加载、未调用。
- 正式根 `E:\QuantKingV3Data`：未访问。
- 旧目录 `E:\QuantKing`：未访问。
- F 盘备份：未访问。
- 仓库内副作用：源码、测试、文档和 marker/离线质量门更新；没有原始真实数据或凭据。

## 当前 source × dataset 结论

| source | datasets | 五项 gate | 稳定运行数 | 结论 |
|---|---|---|---:|---|
| XTQUANT | 六类 | 均未真实评估 | 0 | BLOCKED_EXTERNAL_VALIDATION |
| BAOSTOCK | 六类 | 均未真实评估 | 0 | BLOCKED_EXTERNAL_VALIDATION |
| AKSHARE | 六类 | 均未真实评估 | 0 | BLOCKED_EXTERNAL_VALIDATION |
| TUSHARE | 六类 | 均未真实评估 | 0 | BLOCKED_EXTERNAL_VALIDATION |

没有生成 DifferenceReport。没有读取 Stage 0 的三组旧数据候选，因此它们仍是待授权审计，
没有 `DATA_ONLY` 或 `REJECTED` 结论。

## SDK 与质量结果

- XTQuant/BaoStock/AKShare/Tushare SDK：均未为 S1-07A 加载或新增依赖；没有可报告的本次
  真实执行 SDK 版本。任何 S1-07B/C plan 必须先显示并冻结精确版本。
- R2 回归继续覆盖 Fake signer、结果 attestation、六类 coverage、精确质量目录和 fixed
  sample v2；S1-07A 仍没有 REAL signer。
- R3 攻击测试覆盖手工/seal evidence、路径与 reparse、文件及 verify/persist 间替换、
  合法 SYNTHETIC/BLOCKED 保存，以及两线程、两实例、两进程、重启和 runner 失败后永久消费。
- R4 攻击集覆盖通用 UoW key 参数、私有 DTO/receipt/低层方法、无目录与手工 evidence、
  锁内第二次 verify 后、SQL flush 前、commit 前的 manifest/attestation/Canonical 替换、
  合法 SYNTHETIC/BLOCKED 保存和直接 SQL REAL 插入；`15 passed`。
- R5 独立攻击集覆盖真实子进程普通/嵌套新增、原路径重建、manifest/attestation/Canonical/
  Quality 替换删除重命名、SQLite/SQLAlchemy 直插、无 gate/跨连接/事务后/异常后/过期复用、
  seal 篡改读取、0010 REAL/SYNTHETIC/混合历史隔离、隔离后降级阻断和空表升降级；`18 passed`。
- R6 独立攻击集覆盖 incomplete ZIP 未声明/重复/Canonical/Quality entry 攻击、finalized
  seal 在独占锁前截断/替换、既有同 SHA seal 的锁内完整复验，以及数据库所有者伪造行的
  读侧失败关闭；`8 passed`。
- validation、R3/R4/R5/R6 persistence、assessment repository、DataCenter、
  0010/0011/0012 migration、架构和 Stage 1 文档定向组合：`260 passed`。
- `scripts/quality.ps1` 完整通过。
- 最终离线 pytest：`1113 passed, 2 skipped, 3 deselected`。
- 前端单测：`17 passed`；生产构建通过；Playwright：`1 passed`。
- Ruff、mypy、锁文件、架构边界、Markdown 链接和 `git diff --check` 全部通过。
