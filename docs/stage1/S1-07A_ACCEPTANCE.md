# S1-07A-R4 assessment 目录权威持久化修复交接

状态：S1-07A-R4 修复已提交、等待主验收；24 个单元仍为
BLOCKED_EXTERNAL_VALIDATION；S1-07B/C/D、S1-08 未开始、未获授权。

## 范围与基线

- Stage：Stage 1 / S1-07A-R4。
- R4 基线：`4b03ec26ef93cd7f2f64ad4aaff2434be1d54f38`。
- 只删除调用方可签发的 assessment receipt 绕过，收紧 assessment 持久化入口和 TOCTOU
  边界；R2/R3 signer、coverage、quality、sample 与 authorization claim 均未修改。
- 没有迁移数据、发布 `DatasetVersion`、创建 `SourceOverride`、启用 auto-switch 或开始
  S1-08/Stage 2。

## 首次红测

命令：

```powershell
uv run --offline --frozen pytest tests/integration/data/test_s1_07a_r4_persistence.py tests/tooling/test_architecture_boundaries.py -q -p no:cacheprovider --tb=no
```

R4 首次结果为 `11 failed, 4 passed`。根因为通用 UoW 仍接受调用方提供的
`assessment_persistence_key`，私有 DTO/HMAC receipt 能到达低层写入口；没有专用
directory-only SQLite adapter 和跨最终复验至 commit 的文件锁；数据库也没有阻断
S1-07A 阶段的直接 REAL/PRODUCTION 写入，架构检查尚未识别内部 assessment 写能力。

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

1. 将绝对 assessment directory 约束在受控 `validation_root` 下；
2. 获取独占 assessment 维护锁，并以 Windows 不共享 WRITE/DELETE 的只读句柄锁定最终目录
   已有文件；锁文件位于受控 root 的专用锁目录，跨进程同一 assessment 只能有一个 writer；
3. 使用受控 signer 和 `SignerTrustPolicy` 深验 `_assessment`、全部 runs、Raw、Canonical、
   Quality、DifferenceReport、coverage、fingerprint 和 result attestation；
4. 计算并比较 signer profile 与逐 run result-attestation SHA 摘要，执行锁内第二次完整复验；
5. 在锁仍持有时打开数据库 session/transaction，只写 verifier 返回的精确 assessment；
6. SQL flush 后再次深验；commit 前再次深验；commit 完成后才关闭文件句柄并释放锁。

通用 `DataRepository` 与 `DataSqliteUnitOfWorkFactory` 已删除全部 assessment 写方法和
`assessment_persistence_key` 参数。公共入口不接受 `VerifiedAssessmentEvidence`、DTO、receipt、
seal 或调用方 key；内部 insert 只存在于专用 adapter。架构边界检查禁止其他源码模块导入私有
assessment DTO/receipt/insert 名称。不存在实际 evidence directory 时，不存在应用级写入路径。

数据库行除既有 assessment manifest SHA 和相对路径外，还保存规范
`signer_profiles_json` 与 `result_attestation_sha256s_json`。数据中心仍只读取已经持久化的
assessment 结果。普通 `0011` migration 增加 S1-07A insert gate，只接受
`SYNTHETIC/BLOCKED_EXTERNAL_VALIDATION` 和精确 `["SYNTHETIC"]` signer 摘要，并禁止 assessment
行 UPDATE/DELETE；直接 SQL REAL/PRODUCTION 插入会由 SQLite 拒绝。Alembic 唯一 head 为
`20260729_0011`；`0010 → 0011 → 0010 → 0011` 支持 upgrade/downgrade/upgrade。

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
- validation、R3/R4 persistence、assessment repository、data center、0010/0011 migration、
  Stage 1 文档定向组合：`228 passed`；Ruff 与专用 adapter mypy 通过。
- 完整质量门第一次运行停在 Ruff format check（3 个文件需机械格式化）；格式化后
  `scripts/quality.ps1` 完整通过。
- 最终离线 pytest：`1086 passed, 2 skipped, 3 deselected`。
- 前端单测：`17 passed`；生产构建通过；Playwright：`1 passed`。
- Ruff、mypy、锁文件、架构边界、Markdown 链接和 `git diff --check` 全部通过。
