# S1-07K4 实施记录：三角色重构与仲裁/质量语义

状态：K4A/K4B 离线实现完成并通过完整离线门禁；K4C 第一次代表性真实三源实测（2026-08-15）
经独立验收**不通过**（S1-07K4C-R1），K4C-R1 已完成证据合同与探针执行器修复并生成新零网络
dry-run；**K4C-R2 真实实测已于 2026-08-16 获授权执行完毕**（36/36 成功、证据可离线复算、
`representative_evidence_sufficient=false` 因三个 objective 未在本窗口出现）；**K4C-R3
合同修订（ADR-022）与 Phase B 真实实测已按用户授权完成（2026-08-16）**（9/9 成功，R3
evidence 与 composite evidence 冻结）；**K4C-R4 证据链验证与停牌来源闭链修复（ADR-023）
已交付，并于 2026-08-16 独立验收通过**；**`comparison-thresholds.daily-bar-unadjusted.k4.v1`
（OHLC/`pre_close` 零容差、量额绝对 50/相对 0）已按用户决定接受重新冻结（ADR-024）**，
验收记录 `k4_threshold_acceptance()`（绑定 R2/R3/v1/v2 composite 四条 SHA）。K4D inventory
零读取 dry-run 已冻结（ADR-025，plan SHA `f4fee828…`）；**K4D 真实只读 inventory 已于
2026-08-16 获用户一次性授权执行并成功**（5,208 文件全部可读、零读取错误、与 Stage 0 基线
差异 0/0，evidence SHA `140978ea…`）。**K4D pilot 离线合同、执行器与零外部 I/O dry-run
已就绪并冻结（ADR-026，plan SHA `76e928aa…`）；真实 pilot 两次 attempt 分别于 2026-08-16
（第 6 次调用失败即停）与 2026-08-17（18/18 调用成功后 `PILOT_CANONICAL_COUNT_UNEXPECTED`
守卫触发）获授权执行并失败即停，证据链缺陷已于 2026-08-17 纯离线修复；**旧 XTQuant 日线
迁移已按用户决定拒绝并完成 K4D 决策收口（ADR-027）：不执行第 3 次 pilot、全量诊断或
full；**K4E 独立验收已于 2026-08-17 通过并形成单一 K4 提交
（[S1-07K4_ACCEPTANCE.md](S1-07K4_ACCEPTANCE.md)），下一步是 S1-08R2**。
本文件只记录已实现的事实；2026-08-15 的
证据文件只是历史字节（[dry-run](evidence/k4c_representative_probe_dry_run_2026-08-14.json)、
[attempt a](evidence/k4c_representative_probe_attempt_2026-08-15_a.json)、
[最终 attempt](evidence/k4c_representative_probe_2026-08-15.json)），不再是可接受的验收证据。

## 冻结的 K4 版本合同

| 项 | 值 |
|---|---|
| 解析规则 | `source-resolution.daily-bar-unadjusted.k4.v1`（artifact schema `resolution-artifact.v3`） |
| 来源路由 | `BAO-TX-SINA.k4.v1`（A=BAOSTOCK/BAOSTOCK，B=AKSHARE/TENCENT，C=AKSHARE/SINA） |
| 适配器 | `baostock-akshare-daily-bar.k4.v1`；AKShare 契约 `akshare-daily-bar.v2` |
| 质量规则 | `quality-rules.daily-bar-unadjusted.k4.v1`（含 pre_close/交易日历/缺口/新鲜度门） |
| 网络预算 | 端点 12（A 4 + B 4 + C 4）、C 上限 4、HTTP 上界 20（腾讯×3 + 新浪×2）、BaoStock 协议消息上界 12 |
| 硬超时 | BaoStock 会话 20s；AKShare 受控子进程会话 60s；应用层零重试 |
| 单位策略 | `baostock-0.9.3-returned-units.v1`、`akshare-1.18.83-returned-units.v1`（两者进入 K4 输入指纹，K4C 实测验证通过） |
| 阈值 | `comparison-thresholds.daily-bar-unadjusted.k4.v1`（OHLC/`pre_close` 零容差；量额绝对容差 50 股/50 元、相对 0）**已接受重新冻结（ADR-024，K4C-R4 独立验收通过）**，验收记录 `k4_threshold_acceptance()`；DifferenceReport schema `difference-report-manifest.k4.v4` 保留在代码中。历史 k1 与 k3-candidate 继续可复验；R2/R3/v1/v2 composite 证据中的 `thresholds_pending_reacceptance: true` 是生成时期的字节冻结事实，不随规则状态改写 |
| 输入指纹 | `eod-input-fingerprint.k4.v1`（K4 命令绑定 k4 阈值版本；含 akshare/baostock 单位策略、路由、质量/解析/代码版本）；K2/K3 历史指纹仍绑定 k1 并字节复现 |

## K4A：三角色实现重构

- 删除东财正式 runtime：`ipv4_resolution.py`、`resolution/k3_service.py`、`adapters/eod_k2.py`
  （四角色 dispatch）、`comparison/k3_probe.py`、`comparison/k3_connectivity.py` 与三个 K3 探针脚本。
- `registry.py`：runtime 集合收缩为腾讯 B / 新浪 C；`PRODUCTION_UPSTREAMS=(BAOSTOCK,TENCENT,SINA)`；
  `DataUpstream.EASTMONEY` 枚举与 `(AKSHARE,EASTMONEY)` 对保留，仅供历史 Raw/migration/证据读取。
- 预算收缩为 `K4EndpointBudget`：A/B `freeze_primary`，C(SINA) `preflight_exception`；
  A/B 一致时 C planned/actual 均为 0；不足在首次调用前拒绝，逐调用 `before_endpoint` 守卫。
- AKShare 每次 vendor 调用改经受控子进程（`akshare_worker.py`）+ 父进程硬超时，与 BaoStock 同构；
  每源分别记录 provider/upstream、endpoint、库版本、批次、质量与错误状态。
- `eod_worker.py`：唯一 K4 dispatch；`EodExecutionConfig.production`、frozen 解析、输入指纹、
  release 验证器与 `build_s108_dry_run` 全部收敛到 K4 常量；历史 K2/K3 冻结命令与指纹仍可解析复现。
- DataCenter 发布边界、CLI/Web 投影与 e2e 服务 fixture 更新为三角色。

## K4B：仲裁与质量语义

- 共同缺行不再形成值共识：`_equal`/`_agreement` 只允许实际持有行参与；恰好一个实际来源持有行
  产生 `SINGLE_SOURCE_ROW_ISOLATED` conflict；两个实际行分歧产生 `NO_TWO_SOURCE_CONSENSUS`。
- 多方合法整范围空结果使用 run 级 `ZERO_ROW_LEGAL_EMPTY_CONSENSUS` 原因码（K4 artifact
  summary/manifest 绑定该字段），绝不冒充逐行价格一致。
- DifferenceReport 与 Resolution 共用版本化 Decimal `ThresholdSet`：`thresholds_for` 注册表 +
  `rows_within_threshold` 是唯一比较实现；Resolution 的 `threshold_rule_version` 实际绑定并验证。
- Canonical `pre_close` 对全部服务源恒由完整未复权序列前一行 `close` 派生；vendor 参考昨收只保留
  Raw（AKShare 直出列被显式忽略并测试）；新增 `pre_close_consistency` 阻塞质量规则。
- 版本化交易日历质量门：`exchange_calendar` 参考按版本+内容 SHA 绑定；新增
  `expected_trading_days`（逐证券预期交易日/缺口，冻结上市/停牌例外）与 `freshness` 规则，
  `calendar_membership` 一并进入 K4 质量集；例外经 `QualityRequest.security_exceptions` 冻结。
- Raw/质量失败保留稳定脱敏错误码与受影响计数（既有 `SourceErrorCode`/`BatchFailureInfo` 与
  sanitize 写入路径保持不变并已覆盖测试）。

## K4B 修复：合法零行与冻结例外接入生产 EOD（KB-01/KB-02）

- KB-01 合法零行已接入生产链路：`K4ExpectedSessionResolver`（`adapters/sources/legal_empty.py`）
  基于已发布 `exchange_calendar`（`PublishedReferenceLoader` 在首次网络调用前验证 PUBLISHED/
  dataset/version/content SHA）与冻结 `security_exceptions` 逐 chunk 判定合法空；任何剩余预期
  交易日都返回 `None`，上游零行随后按 `UNEXPECTED_EMPTY` 失败。构造时强制验证请求区间内每个
  涉及交易所、每个自然日在日历中恰好一条记录：缺日期或缺交易所抛
  `CalendarReferenceIncompleteError`，worker 映射为稳定 `CALENDAR_REFERENCE_INCOMPLETE`，
  在首次网络调用前阻断（日历缺口绝不读作休市，`security_exceptions` 不能掩盖缺口）。粒度下沉到
  每个 `FetchChunk`：Raw manifest 以 `legal_empty_by_chunk` 逐 chunk 记录分类；只有整批零行且
  每 chunk 均合法时 `IngestionBatch.empty_result` 才表示批次级空结果，混合批次不会整批标空也不
  静默丢弃合法空 chunk。
- KB-01 质量门补强：新增阻塞规则 `calendar_coverage`（每个预期交易所×每个自然日恰好一条日历记录），
  `expected_trading_days`/`freshness` 对空表不再直接放行，而是按冻结日历与例外重算每只证券的
  预期交易日；全部证券预期集合为空才通过，伪造 `empty_result` 或截断日历都无法绕过质量门。
- KB-02 `security_exceptions` 进入冻结合同：新增 `eod-execution-config.v4`（仅 K4），
  `EodDatasetConfig`/`FrozenEodDataset` 规范化保存例外（代码∈scope、ISO 日期、start<=end、
  排序唯一且拒绝重叠）；v2/v3 历史 config、fingerprint 和字节保持原样。例外同时进入 source
  quality、final quality、合法空分类器与 dry-run；`config_sha256` 与 K4 输入指纹绑定例外，
  retry 必须携带完全相同的冻结例外。
- Runtime 提取 `PublishedReferenceLoader` 并同时注入 `QualityRunner` 与 `EodWorkerHandler`；
  K4 Job 缺失或无法验证日历引用时 fail-closed（`CALENDAR_REFERENCE_*`）。

## K4C：第一次实测、独立验收不通过与 R1 修复

- 冻结计划：`comparison/k4c_probe.py` 固定 6 只代表性证券（深主板 000001.SZ、沪主板
  600000.SH、除权史 000858.SZ、创业板 300750.SZ、科创板 688981.SH、高价量额边界
  600519.SH）、`2026-06-11..2026-08-09` 窗口、15 日 lookback、31 日/1 证券分块、36 次端点
  调用（A/B/C 各 12）、硬超时（BaoStock 20s、AKShare 60s）、零重试、失败即停。
  `build_k4c_representative_dry_run` 输出零网络计划（历史字节：
  [evidence/k4c_representative_probe_dry_run_2026-08-14.json](evidence/k4c_representative_probe_dry_run_2026-08-14.json)）。
- 第一次真实执行由旧 marker `K4C-REPRESENTATIVE-2026-08-14-v1` 驱动，共三次尝试：a（脚本
  缺陷，3/36 调用，供应商零失败）、b（36/36 调用全部成功但**没有产生任何证据文件**）、
  第三次 36/36 全部 SUCCEEDED。attempt b 没有证据，这是不可修复的历史缺口，只能如实记录，
  不得伪造或事后补写；本记录是它的唯一书面记载，不能声称 attempt b 已记录于任何证据文本。
  旧 marker 已退役，不得再次使用。
- 第一次实测事实（历史证据 JSON）：36/36 SUCCEEDED、库版本/契约/单位与冻结策略一致、
  三源 246/246 共同持有、OHLC/`pre_close` 全部一致、量额差异来自腾讯 100 股/100 元舍入。
  “36 次调用成功”只证明调用成功，不等于代表性验收通过。
- **S1-07K4C-R1 独立验收不通过**，三个阻断：
  1. 代表性覆盖没有实际验证：`representative_evidence_sufficient` 在 36 次调用完成后即置
     true，没有验证普通/除权/停牌/低流动性/量额边界/缺行空结果/沪深代码段是否实际观测；
     最终证据 246 行全由三源持有，至少缺行/空结果没有出现。
  2. 授权调用与失败留证不闭环：同一旧 marker 下三次尝试，attempt b 没有证据文件。
  3. 阈值极值不能离线复算：divergence 只保留前 20 条，volume/amount 的最大绝对差 50
     所在行都未保留（保留行最大值只有 46 与 49.38），content_sha256 没有对应 raw payload
     文件，50 的来源无法复算。
- R1 离线修复（本文件范围，零真实网络）：
  - 新证据合同 `src/quantking/data/comparison/k4c_evidence.py`：`planned_objectives` 与
    `observed_objectives` 分离；8 个必需 objective 各有冻结、可执行的判定标准（如除权用
    vendor preclose 相对差 ≥0.005、低流动性 amount ≤2.5e8、量额边界 amount ≥1e9、停牌用
    横截面缺日），未观测给出稳定原因码；`representative_evidence_sufficient` 只在全部计划
    执行、无失败/未调用、全部必需 objective 实际观测、契约检查通过、比较证据经离线
    verifier、无未记录 attempt、无越权调用之后才为 true，并在 cross-source measurement 与
    objective 校验完成后计算。
  - 一次性 attempt ledger `K4CAttemptLedger`：marker 绑定 plan_sha256/调用预算/输出目录/
    authorization_id；同一 authorization_id 只能一个 attempt（排他 attempt header 在第一次
    网络调用前创建）；每调用完成立即追加并 flush 到 invocations.jsonl；进程异常后仍可数出
    invoked/not-called；重放拒绝发生在 gateway.fetch 之前（合成测试证明）；旧 marker 拒绝。
  - 可离线复算的 artifact：全部规范化比较行（`artifacts/comparison-rows.json`，每
    role/symbol/date 含 OHLC/pre_close/volume/amount）、每次调用的 raw envelope
    （`artifacts/raw/<seq>.json`）、绑定所有文件 SHA 的 artifact-manifest；每字段始终保留
    max absolute/max relative 行及其三源原始规范化值，无 top-N 截断。
  - 离线 verifier `K4CVerifier`：重算 held histogram、exact/divergent 计数、每字段极值、
    objective 覆盖、阈值覆盖、contract 事实、artifact SHA 与调用数/预算，并拒绝任何与
    evidence 摘要不一致的值（不信任预填摘要）。
  - 阈值纪律：`comparison-thresholds.daily-bar-unadjusted.k4.v1` 与
    `difference-report-manifest.k4.v4` 在 R1 时点代码保留但状态为**待重新验收**；
    （后经 K4C-R2/R3/R4 证据链，已于 2026-08-16 经 K4C-R4 独立验收通过并按用户决定
    接受重新冻结——见 ADR-024。）新真实证据通过离线 verifier 前不得宣称独立验收通过，
    不得作为 K4E/S1-08 前置。
  - 新零网络 dry-run：`scripts/probe_s1_07k4c_r2_dry_run.py` →
    [evidence/k4c_r2_probe_dry_run_2026-08-16.json](evidence/k4c_r2_probe_dry_run_2026-08-16.json)
    （新 marker `K4C-REPRESENTATIVE-R2-2026-08-16-v1`、plan_sha256
    `8ea2ea1f470d781309a59e4526deffe9e36dded6f6146daa29db72c4e64b3e65`、36 次调用预算、
    逐 objective 冻结判定与证券/区间、输出目录 `docs/stage1/evidence/k4c-r2-attempts`、
    零正式数据根写入、`requires_real_network_authorization=true`）。
  - 真实探针执行器重写为 R2 版（`tests/compatibility/test_k4c_representative_real.py`）：
    仅在新 marker + 新 authorization_id + 输出目录三个环境变量同时就绪时运行；默认 pytest
    与门禁跳过；evidence 只在离线 verifier 重算通过后输出。
  - 历史三文件字节保护测试与 12 项反向测试见 `tests/data/test_k4c_historical_evidence.py`
    与 `tests/data/test_k4c_r1_evidence.py`。
- **K4C-R2 真实实测（2026-08-16，已获授权并执行）**：marker
  `K4C-REPRESENTATIVE-R2-2026-08-16-v1` 的第一次 authorization_id
  `K4C-R2-cab803399515`（attempt `attempt-3135a90149554d29`）已消费；36/36 端点调用
  SUCCEEDED（65.1s、零重试、失败即停、零正式数据根写入）。attempt 目录
  `docs/stage1/evidence/k4c-r2-attempts/K4C-R2-cab803399515`（attempt-header.json、
  36 行 invocations.jsonl、comparison-rows.json、36 个 raw envelope、
  artifact-manifest.json），证据文件
  [evidence/k4c_r2_representative_probe_2026-08-16.json](evidence/k4c_r2_representative_probe_2026-08-16.json)
  （SHA-256 `217390b53e03345ac84a97739a88cb28b05299f9d2d2bc9ec036d7af89381835`）。
  实测结论（全部由离线 verifier 从 artifact 重算）：
  - 契约检查全过（单位/未复权/完整/日期/字段/库版本）；246 行三源共同持有；OHLC/`pre_close`
    246/246 一致；量额差异全部为腾讯 100 股/100 元舍入，**极值 50 股/50 元连同三源原始值与
    行位置完全可离线复算**（volume@600000.SH/2026-07-15、amount@000858.SZ/2026-07-14），
    候选 k4 阈值覆盖全部观测差异（`threshold_coverage.covered=true`）。
  - objective 实测：normal/ex_rights（000858.SZ/2026-07-16，vendor preclose 73.82 对派生
    76.4，相对差 3.38%）/volume_amount_boundary/沪深代码段 observed；suspension/
    low_liquidity/missing_or_empty 在本窗口未出现，如实记为未观测。
  - `representative_evidence_sufficient=false`（三个 `OBJECTIVE_UNOBSERVED` 原因码）——
    这是冻结判定下的诚实结果，不是失败；阈值重新冻结建议与“未观测 objective 如何处理”
    均须由独立验收决定。同一 authorization_id 已被 ledger 锁定，不可重放。

## K4C-R3：raw→comparison 闭环、上市前合法空合同（ADR-022）、Phase B 实测与 composite evidence

- **raw→comparison 离线重建闭环**（`k4c_evidence.reconstruct_canonical_rows_from_raw` +
  closure 检查）：verifier 现在从每个 raw envelope 的 provider payload 重建 Canonical 行
  （BaoStock 字符串行、AKShare records；腾讯 `sz000` 手×100 按冻结单位策略、amount 元、
  `pre_close` 按完整未复权序列前一日 close 派生、裁回目标窗口），并与 comparison-rows.json
  逐键、逐字段比较；`response.records` 与预填摘要一律不信任。新增稳定错误码：
  `RAW_COMPARISON_KEY_MISMATCH`、`RAW_COMPARISON_VALUE_MISMATCH`、
  `RAW_UNIT_RECONSTRUCTION_MISMATCH`、`RAW_PRE_CLOSE_RECONSTRUCTION_MISMATCH`、
  `JOURNAL_RAW_SHA_MISMATCH`、`RAW_PLAN_BINDING_MISMATCH`、`UNREFERENCED_RAW_ARTIFACT`；
  另有 journal↔raw 缺失（`ARTIFACT_MISSING`）与 SHA 复验。**不可变 R2 attempt 在新
  verifier 下复验通过（reasons=()，36/36 调用、重建行与 comparison 完全一致）**；R2
  plan_sha256/authorization_id/attempt_id/evidence SHA/manifest SHA 保持字节不变
  （`tests/data/test_k4c_r3_evidence.py::test_r2_baseline_shas_are_frozen`）。
- **K4C-R3 补充样本合同（用户接受的合同修订，ADR-022，2026-08-16）**：只补三项未观测
  objective（suspension/low_liquidity/missing_or_empty）。每个样本必须固定 exact
  symbol/trade_date/最小窗口/交易所/三源预期持有-缺失状态/独立参考（source+版本或文件
  +SHA）/成功判定/失败原因码；禁止空 `symbols`、无日期 objective、机会型覆盖与事后挑选。
  冻结判定：停牌=交易日横截面缺日（禁周末/节假日/上市前/退市后冒充，参考仅限交易所公告
  或供应商交易状态）；低流动性=沿用 R2 冻结边界 `max(A,B,C amount) ≤ 2.5e8` 且三源同持
  一行。缺失场景的临时 partial-missing 合同被**上市前合法整范围空**取代
  （`legal_full_range_empty_pre_listing`）：解释 `legal_pre_listing_empty`（永不
  `vendor_partial_missing`）、A/B/C 全部 `EMPTY`、成功码
  `LEGAL_EMPTY_PRE_LISTING_OBSERVED`、零行原因码 `ZERO_ROW_LEGAL_EMPTY_CONSENSUS`；
  请求窗口不得跨上市日、必须带上市参考+日历参考、工作日校验、`K4ExpectedSessionResolver`
  无预期交易会话；任一来源 FAILED/PARTIAL/有行、raw 有行而 comparison 空、error→empty、
  与停牌重复计数均失败（样本级拒绝码 `R3_SAMPLE_VENDOR_PARTIAL_MISSING_RETIRED` 等）。
  三组样本已冻结（`k4c_r3_frozen_samples()`，独立参考文件 SHA 冻结）：
  - 停牌 688237.SH/2026-07-16 窗 3（`73e9cbec…`）
  - 低流动性 688237.SH/2026-07-15 窗 1（`060aac79…`）
  - 上市前空 688825.SH/2026-07-24 窗 1、上市日 2026-07-27（`4ebd0410…`）

  样本校验、`observe_k4c_r3_objectives`（样本绑定观测，机会型覆盖拒绝）、
  `compute_k4c_r3_sufficiency` 与 plan/dry-run 生成器已实现（`k4c_r3_plan_payload` 等，
  预算固定 9 次端点调用：A/B/C 各 3；HTTP 上界 15；BaoStock 协议消息 9；20s/60s 硬超时；
  零重试；失败即停；输出目录 `docs/stage1/evidence/k4c-r3-attempts/<authorization_id>`）。
  最终 R3 零网络 dry-run 已生成并冻结：
  `docs/stage1/evidence/k4c_r3_probe_dry_run_2026-08-16.json`
  （SHA `174c9546…`，plan_sha256 `38c88f40…`）。
- **K4C composite evidence**（`s1-07k4c-composite-evidence.v1`，`K4CCompositeVerifier`）：
  只通过 SHA 引用 R2/R3（plan_sha256、authorization_id、attempt_id、evidence SHA、
  artifact manifest SHA），重读证据文件与 attempt 目录、分别重跑 closure verifier、
  校验两 authorization_id 不同且各只有一次 attempt、合并 objective 事实（冲突
  fail-closed：同 objective 同 (symbol,date) 不同 fact → `CONFLICTING_OBJECTIVE_FACTS`；
  组件行键冲突 → `COMPONENT_ROW_CONFLICT`）、对全部可比较 held rows 重算直方图/
  exact/divergent/极值/阈值覆盖；`representative_evidence_sufficient=true` 与
  `threshold_reacceptance_eligible=true` 是两个独立字段，只有 8/8 objective 实际观测、
  组件验证全部通过且候选 0/50 阈值覆盖全部观测差异时才为 true。
- **composite 合并行再观测**：R3 样本在其自身 attempt 内未观测而合并行可观测时（停牌样本
  的横截面反例只能来自不可变 R2 行），`merge_objective_records_with_r3_reobservation`
  按同一冻结样本合同在合并行上再观测并通过 `validate_k4c_r3_objective_records` 越界拒绝；
  上市前空样本需要日历证明，绝不在此路径伪造观测。停牌与上市前空是两个不同 objective/
  样本/证据类型，永不重复计数。
- **Phase B 实测与 K4C-R3-R1 修复（2026-08-16，三次一次性授权全部消费）**：
  - 第 1 次尝试 `K4C-R3-4f9c2e7a1b08`：第 1/9 次失败即停——BaoStock 对 688237.SH 返回
    07-16 停牌占位行（OHLC 填充昨收、`volume=""`/`amount=""`、`tradestatus=0`），冻结
    语义报 `BAOSTOCK_SCHEMA_DRIFT`；journal/header 失败证据保留。
  - 有界诊断（用户授权 1 次 BaoStock 调用）捕获原始行形态，证据
    `docs/stage1/evidence/k4c_r3_diagnostic_baostock_688237_2026-08-16.json`
    （SHA `c31febc8…`）。
  - **K4C-R3-R1 修复**：`BaostockDailyBarAdapter` 与
    `reconstruct_canonical_rows_from_raw` 跳过 `tradestatus=0` 且 volume/amount 均为空的
    停牌占位行（不产生 bar、不喂 `pre_close` 推导、raw 保持逐字保留）；`tradestatus=0` 带
    量、`tradestatus=1` 空量仍报 schema drift。合同版本与单位策略不变（R2 字节证据继续
    可复验）。反向测试 4 项（`tests/data/adapters/test_baostock_adapter.py`：占位行丢弃、
    不污染后续 `pre_close`、停牌带量 drift、交易日空量 drift）+ 重建跳过测试。
  - 第 2 次尝试 `K4C-R3-7c51a3d9e2b4`：前 6 次 688237 调用全部成功（停牌修复在真实数据
    上验证通过），第 7 次因执行器未把冻结日历的合法空分类传给归一化而失败即停
    （`UNEXPECTED_EMPTY`）；执行器修复（仅对 688825.SH 调用传入 resolver 分类）。
  - 第 3 次尝试 `K4C-R3-2b8e5f14a6c9`：**9/9 成功、evidence 经离线 verifier 复算通过**
    （SHA `1632ee8b…`、manifest SHA `2b2f919b…`、attempt_id `attempt-9ced23d1844541fd`）。
    观测：低流动性 688237.SH/2026-07-15 三源同持且金额 1.68e8 ≤ 2.5e8 ✓；上市前空
    688825.SH 三源零行+日历证明 → `LEGAL_EMPTY_PRE_LISTING_OBSERVED` ✓；停牌在 R3 本地
    无横截面反例，如实 `SUSPENSION_NOT_OBSERVED`（sufficient=false 如实记录）。
  - **composite evidence**（`docs/stage1/evidence/k4c_composite_evidence_2026-08-16.json`，
    SHA `910010da…`，`scripts/build_k4c_composite_evidence.py` 离线构建并经
    `K4CCompositeVerifier` 验证）：合并行再观测用不可变 R2 行（其六只证券持有 07-16）作
    反例观测停牌 → **8/8 objective 成立、`representative_evidence_sufficient=true`、
    `threshold_reacceptance_eligible=true`**（held_rows 248 = R2 246 + R3 2；金额差异
    ≤45 元在候选 0/50 阈值覆盖内）。k4 阈值正式重验收仍须独立验收。
- 反向测试：`tests/data/test_k4c_r3_evidence.py`（合同修订 + Phase B 后 52 项：R2 重建
  一致、comparison/raw 篡改拒绝、journal SHA 不符、单位/pre_close 错误、多/少行、未引用
  raw、样本合同拒绝路径（含 vendor_partial_missing 退役、缺上市/日历参考、错上市日、
  窗口跨上市日、周末冒充、非 EMPTY 角色）、观测路径（停牌反例、无反例不观测、上市前缺行
  不冒充停牌、低流动性边界、三源零行成功、任一 FAILED/有行/不完整拒绝、无日历证明
  fail-closed、停牌与上市前空不重复计数）、`compute_k4c_r3_sufficiency`、
  raw 重建跳过停牌占位行、composite 缺失 objective→false、8/8→true+eligible、SHA 篡改
  拒绝、R3 raw 有行而 comparison 空拒绝、默认 pytest 零网络（R2/R3 执行器与诊断全部
  skip）、R2/R3/dry-run/日历/诊断/composite 基线 SHA、真实 R3 composite 离线复验）。

## K4C-R4：证据链验证与停牌来源闭链修复（ADR-023，离线完成）

独立验收发现三个阻断，本轮仅做离线证据链修复（零供应商网络、零正式数据根写入）：

- **`K4CVerifier.verify` 全字段绑定验证**：除既有统计摘要外，现在验证
  `schema_version`（R2/R3 各自冻结 schema）、identity（`authorization_id`/
  `attempt_id`）、`authorization_marker`、`plan_sha256`、`invocation_budget`——全部从
  attempt header 与当前冻结 verifier 合同重算；`artifact_manifest_sha256` 直接对
  manifest 文件计算；`artifacts_root` 规范化后必须精确指向被验证 attempt 目录；
  `application_retries`/`fail_fast`/`writes_formal_data_root` 与冻结合同一致；
  `failure` 与 journal 的 FAILED 事实一致（成功 attempt 必须为 null）；`sufficiency_reasons`
  与重算逐项一致；`planned_objectives` 等于冻结 R2 objectives 或 R3 冻结样本序列；
  R3 `calendar_proofs` 等于冻结日历常数导出的规范证明；顶层字段白名单之外的一律拒绝。
  稳定错误码：`EVIDENCE_SCHEMA_MISMATCH`、`EVIDENCE_HEADER_BINDING_MISMATCH:<field>`、
  `EVIDENCE_CONTRACT_BINDING_MISMATCH:<field>`、`EVIDENCE_MANIFEST_SHA_MISMATCH`、
  `EVIDENCE_ARTIFACT_ROOT_MISMATCH`、`EVIDENCE_SUMMARY_MISMATCH:<field>`、
  `EVIDENCE_PLANNED_OBJECTIVES_MISMATCH`、`EVIDENCE_CALENDAR_PROOF_MISMATCH`、
  `EVIDENCE_UNKNOWN_FIELD:<name>`。真实 R2/R3 evidence 在新验证下通过。
- **`K4CCompositeVerifier` schema 化**：构造参数 `schema_version`（默认历史 v1）。
  v1 保持可复验（含 `sufficiency_reasons` 一致性与已声明 component 与冻结引用一致的
  校验）；v2（`s1-07k4c-composite-evidence.v2`）要求 composite 自带的
  `r2_component`/`r3_component` 与冻结 `*_component_reference().to_dict()` 完全一致
  （任一字段变化、缺失或类型错误 → `COMPOSITE_COMPONENT_REFERENCE_MISMATCH:R2/R3`）、
  `verification_contract_version` 冻结、schema 错误 → `COMPOSITE_SCHEMA_MISMATCH`；
  这些失败使 `ok=false` 且不被 structural 过滤忽略。
- **停牌来源闭链（provenance hardening）**：新增官方参考
  `docs/stage1/evidence/k4c_r4_references/suspension_688237.SH_2026-07-16_sse.json`
  （证券 688237.SH、公告编号 2026-031、停牌起始 2026-07-16、来源类型
  `exchange_announcement`、上交所 URL `https://static.sse.com.cn/…/688237_20260716_Y6W3.pdf`、
  `provenance_hardening_only=true`；未下载远端 PDF、未伪造远端 SHA），本地 JSON SHA-256
  `cca0e7d1…` 冻结并由 v2 composite 精确绑定。停牌 objective 成立条件收紧为五条：①样本
  即 688237.SH/2026-07-16；②A/B/C canonical/comparison 均无该日；③R2 同日横截面反例由
  A/B/C 三源共同持有；④官方参考通过 SHA/证券/日期/公告编号/来源类型检查（媒体 URL 冒充
  `exchange_announcement` 拒绝）；⑤BaoStock raw sequence 1 的 2026-07-16 行
  `tradestatus=0` 且 volume/amount 为空。任一缺失返回 `SUSPENSION_NOT_OBSERVED` 或具体
  来源错误码（`SUSPENSION_REFERENCE_MISSING`、`SUSPENSION_RAW_FACT_MISSING`、
  `SUSPENSION_RAW_STATUS_MISMATCH`、`SUSPENSION_REFERENCE_*_MISMATCH/INVALID` 等）。
  实现：`validate_k4c_r4_suspension_reference`、`k4c_r4_suspension_raw_fact`、
  `_observe_r3_sample` 的 `require_suspension_provenance` 分支与
  `merge_objective_records_with_r3_reobservation` 透传（再观测失败的具体来源码进入
  merged 汇总，不再被通用码吞掉）。
- **R4 v2 composite**：`docs/stage1/evidence/k4c_r4_composite_evidence_2026-08-16.json`
  （SHA `87b04863…`，`scripts/build_k4c_composite_evidence.py --schema v2` 离线构建+验证）
  只经 SHA 引用不可变 R2/R3；声明“offline acceptance repair layer, not a new real
  network attempt”；重算 248 个三源共同持有行、8/8 objective、全部字段
  exact/divergent、最大绝对/相对差与 0/50 阈值覆盖；输出 `representative_evidence_sufficient=
  true`、`threshold_reacceptance_eligible=true`、`sufficiency_reasons=[]`、
  `r2_component`/`r3_component`、`suspension_reference`（含 SHA `cca0e7d1…`）、
  `suspension_raw_fact`（tradestatus=0）、`verification_contract_version`。
- **历史保护**：R2 evidence `217390b5…`、R3 evidence `1632ee8b…`、R3 manifest
  `2b2f919b…`、旧 v1 composite `910010da…` 全部字节不变；旧 v1 composite 保留为历史
  产物并通过字节保护测试（v1 重算与冻结文件字节一致）。未修改任何 R2/R3 attempt、
  raw、journal、manifest、evidence、dry-run、计划 SHA 或参考文件。
- 新增反向测试 `tests/data/test_k4c_r4_evidence.py`（63 项）：R3 evidence 15 字段
  参数化篡改（每字段对应稳定错误码）+ 未知字段 + 缺字段；composite v2 组件 8 字段×
  R2/R3 参数化篡改、schema/sufficiency_reasons/verification_contract_version 篡改、
  删除组件、组件类型错误、suspension_reference SHA 篡改/删除、raw_fact 篡改；停牌来源
  参考 8 类参数化校验 + 闭链五条件成功/失败路径（角色持有目标日、反例非三源、缺参考、
  错日期、缺 raw、tradestatus≠0、占位行带量）+ 真实 R3 raw 占位行与 comparison 无该日
  断言；真实 R3 evidence 与真实 R4 composite 完全离线验证；历史 SHA 冻结。
- **阈值重新冻结已接受（ADR-024，2026-08-16）**：K4C-R4 独立验收通过后，用户接受重新
  冻结 `comparison-thresholds.daily-bar-unadjusted.k4.v1`（OHLC/`pre_close` 零容差、
  量额绝对 50/相对 0）。规则值自 K4 候选起未变，仅状态由待重新验收转为已接受；冻结的
  验收记录 `k4_threshold_acceptance()`（`K4ThresholdAcceptanceRecord`：规则版本、验收日
  2026-08-16、验收方、各字段容差、四条 SHA 绑定证据引用）写入 `thresholds.py` 并进入
  唯一注册表。历史 evidence 中 `thresholds_pending_reacceptance: true` 是生成时期的字节
  冻结事实，保持原样并可字节一致复验（`test_frozen_evidence_coverage_payload_stays_
  byte_identical`）。

## K4D：旧 XTQuant 日线 DATA_ONLY 迁移（已收口：评估完成、迁移拒绝，ADR-027）

- inventory/pilot/full 拆分为三组独立授权 marker（`K4D_INVENTORY/PILOT/FULL-*`），mode↔marker 绑定
  强制校验，K3C 历史 marker 仅保留 FULL 兼容；pilot 强制固定证券子集并计数 deferred 文件/字节。
- 新增 `LegacyRecheckEngine`（`adapters/legacy_recheck.py`）：staged X 行对 A/B/C 复核，逐行分类
  `X_A_CONSENSUS` / `SERVICE_CONSENSUS_REPLACED_X` / `QUARANTINED` / `X_ONLY_UNVERIFIED` 并守恒计数，
  与 Resolution 共用同一 `ThresholdSet`；管理导入仍永不发布 Warehouse，只读旧根、指纹守恒不变。
- 5,208 文件 / 527,708,270 字节只是 Stage 0 元数据基线；真实 inventory/pilot/full 不得合并授权。

### K4D-I0：inventory 零读取 dry-run 与 closed-scan 执行合同（2026-08-16，ADR-025）

- `quantking-legacy-import inventory --dry-run` 新增（合同
  `legacy-inventory-dry-run.k4d.v1`，`LegacyInventoryDryRun` 纯域对象，零文件系统访问）：
  不调用 discover/inventory/closed_scan/os.walk，不对旧根 exists/is_dir/resolve/stat/open/
  rglob，不创建 staging，不读 Parquet；使用 real marker
  （`S1-07K4D-INVENTORY-REAL-READONLY-V1`）时同样零访问。计划为单行 canonical JSON，包含
  `reads_source=false`、`writes_source=false`、`writes_staging=false`、
  `publishes_warehouse=false`、`service_network_invocation_upper_bound=0`、
  `application_retries=0`、`requires_new_authorization_for_execution=true`、
  `candidate_rule="recursive *.parquet"`、`follow_symlinks_or_junctions=false`、Stage 0 基线
  （5,208 文件 / 527,708,270 字节，`stage0_baseline_role=difference-comparison-only`，仅差异
  比较不是通过条件）与稳定 `plan_sha256`（对去掉该字段后的 canonical 载荷计算）。
- 冻结证据 `docs/stage1/evidence/k4d_inventory_dry_run_2026-08-16.json`：文件 SHA-256
  `46272de4…`、plan_sha256 `f4fee828…`，已在 evidence README 登记；后续真实 inventory 授权
  必须引用该 plan SHA 与 real marker。
- 实际 inventory 执行合同（已就绪、**未执行**）：CLI `inventory`（无 `--dry-run`）改走
  `LegacyInventoryPlanner.closed_scan()`——discover → 候选 size/mtime 元数据快照 → 逐文件
  inventory（每文件读取前后 stat 比对 + 前后双 SHA-256 防扫描期间变化）→ 再 discover →
  再快照。候选新增、删除、消失或 size/mtime 变化时抛 `LegacyCandidateSetChanged`，整个
  inventory 判为失败，不产生可供 pilot 使用的成功清单（CLI 退出码 1、
  `LEGACY_SOURCE_ERROR`）。输出保持 `legacy-inventory.k3c.v1` schema，pilot 的
  `inventory_from_payload` 可直接消费，K3C 历史清单 payload 继续可读。inventory 不写
  staging、不复制文件、不做 Canonical 转换、不调用 A/B/C。
- 测试见 `tests/data/test_k4d_inventory.py`（dry-run canonical 输出、real marker 零 FS 访问、
  不调 discover/walk/Parquet、不建 staging、跨 phase marker 拒绝、closed_scan 与
  discover+inventory 等价、候选新增/删除/元数据变化失败、CLI 失败路径无成功载荷、冻结
  evidence 字节一致）。
- **真实只读 inventory 执行（2026-08-16，用户一次性授权已消费）**：按 dry-run 的 plan SHA
  （`f4fee828…`）与 marker `S1-07K4D-INVENTORY-REAL-READONLY-V1` 对
  `E:\QuantKing\data\kline\1d` 执行 `closed_scan` 一次，退出码 0、197.1s、零重试、零
  staging 写入、零网络、旧根只读。结果（离线复验 canonical + `inventory_from_payload`
  回读）：**file_count=5,208、total_bytes=527,708,270，与 Stage 0 基线差异 0/0**；
  readable_rows=16,786,177；零 `read_error`（含 `FILE_NOT_FOUND`/
  `SOURCE_MUTATED_DURING_READ`/`PARQUET_READ_ERROR` 均未出现）；duplicate_key 总数 0；
  全部 5,208 文件共享同一 Parquet schema（`code: string`、`date: string`（`yyyymmdd`）、
  `open/high/low/close/volume/amount: double`，含 pandas metadata，**无 `pre_close` 列**）；
  date 覆盖 19901219..20260603、code 覆盖 000001.SZ..689009.SH。closed scan 的前后
  discover 与 size/mtime 快照一致（候选集合未变化）。证据
  `docs/stage1/evidence/k4d_inventory_2026-08-16.json`（3,366,115 字节单行 canonical JSON，
  文件 SHA-256 `140978ea…`、source_fingerprint `af42dd43…`）冻结并新增字节保护测试
  （`test_frozen_k4d_real_inventory_evidence_is_byte_stable_and_readable`）。pilot 前须另行
  冻结 `date` 字符串日期解析与 `pre_close` 缺列的显式语义映射；该授权不扩展到 pilot/full。

## 未完成与授权边界

- K4C 第一次实测独立验收不通过；R1 离线修复已交付；K4C-R2 真实实测已于 2026-08-16 执行
  （authorization_id `K4C-R2-cab803399515` 已消费，36/36 成功，sufficient=false 如实记录）。
  K4C-R3 Phase B 已按用户授权完成（三次一次性 authorization_id 全部消费：两次失败即停
  证据保留 + 一次 9/9 成功），R3/composite evidence 冻结。K4C-R4 证据链与停牌来源闭链
  修复已离线交付（v2/R4 composite 冻结，旧 v1 保留为历史产物），**K4C-R4 已于 2026-08-16
  独立验收通过；`comparison-thresholds.daily-bar-unadjusted.k4.v1` 已按用户决定接受重新
  冻结（ADR-024）**。K4D inventory 零读取 dry-run 已冻结（ADR-025，plan SHA
  `f4fee828…`）；**K4D 真实只读 inventory 已于 2026-08-16 获授权执行一次并成功**（5,208
  文件、零读取错误、基线差异 0/0，source_fingerprint `af42dd43…`；该授权已消费，不得
  重放）；**K4D pilot 离线合同/执行器/零外部 I/O dry-run 已就绪并冻结（ADR-026，plan
  SHA `76e928aa…`），两次真实 pilot attempt 均已获授权执行并失败即停（2026-08-16 第 6 次
  调用失败；2026-08-17 18/18 调用成功后 `PILOT_CANONICAL_COUNT_UNEXPECTED` 守卫触发），
  两次授权均已消费、不得重放**；**旧 XTQuant 日线迁移已按用户决定拒绝并完成 K4D 决策
  收口（ADR-027）：第 3 次 pilot、全量诊断与 full 均不执行**；K4E 独立 agent 验收与独立
  K4 提交已于 2026-08-17 完成（见文末 K4E 节）；
  S1-08 均未完成。
- K4 质量规则要求冻结配置绑定已发布 `exchange_calendar` 版本（版本+内容 SHA）；正式 EOD 在
  R2/R3 冻结该引用前会按 fail-closed 拒绝运行——这是预期行为，不构成静默降级。
- 真实网络、`E:\QuantKing` 读取、`E:\QuantKingV3Data` 写入、F 盘访问均须按任务单独授权；
  旧 K4C marker 的 36 次调用授权已消耗并退役；R2 marker 的第一次 authorization_id 已消费，
  ledger 拒绝其重放，任何新的真实调用都需要新的 authorization_id 与授权。本实施不授权任何
  其他外部操作，也不开始 Stage 2 或 S1-08R2 的正式运行。

## 离线门禁证据（K4E 前置，非验收）

- K4A/K4B 时 `scripts/quality.ps1` 全链通过（退出码 0）：锁文件一致性、Ruff lint/format、
  mypy 127 源文件、全部离线 pytest（1077 passed、3 skipped、3 deselected）、架构边界、
  Markdown 链接、前端 typecheck/17 unit/production build/Playwright e2e 1 passed、
  `git diff --check`。
- K4C 冻结改动后复验：`uv lock --check --offline` 通过、Ruff lint/format 通过（254 files）、
  mypy 127 源文件通过、架构边界通过、Markdown 链接通过、`git diff --check` 通过、前端
  typecheck 通过；离线 pytest（与门禁相同 marker 选择）1086 passed、4 skipped、3 deselected，
  3 个 spawn/multiprocessing 并发测试因本会话沙箱的命名管道限制无法运行（EPERM，非代码失败，
  相关代码未改动），前端 unit/build/e2e 因同一沙箱限制未在本会话重跑（前端代码未改动）。
  上述受限项需在不受该沙箱限制的环境中按 `scripts/quality.ps1` 复跑。
- K4B 复验聚焦：KB-01/KB-02 的 resolver 单测（含日历完整性：缺日期/缺交易所 fail-closed、
  全休市完整日历允许合法空、例外不掩盖缺口）、质量规则日历覆盖/空表重验、v4 例外 round-trip/
  篡改拒绝/指纹变化、历史 v2/v3 字节复现、EOD Worker 合法零行/伪造空/单源空/混合批次/例外
  注入/不完整日历零网络零发布集成测试全部通过。
- K4C-R1 复验聚焦（本轮）：`tests/data/test_k4c_r1_evidence.py`（12 项反向测试：
  36 次调用成功但缺 objective→sufficient=false；planned 无 observed→false；缺行/空结果未
  出现→false；objective 越界拒绝；全满足才 true；>20 条 divergence 极值保留；篡改汇总被
  verifier 拒绝；comparison/raw artifact 缺失或 SHA 篡改被拒绝；同一 authorization_id 重放
  在首次 gateway.fetch 前拒绝；第 N 次失败记录 N 次调用与 not-called 且无后续调用；50 通过/
  50.000001 拒绝与 OHLC 零容差）、`tests/data/test_k4c_historical_evidence.py`（三个历史
  K4C 文件字节不变、attempt b 无证据文件、v1 计划几何冻结）。
- 真实网络守卫（`real_k4c_probe`）在离线门禁中全部跳过；旧 marker
  `K4C-REPRESENTATIVE-2026-08-14-v1` 已退役；新 R2 真实执行须
  `QUANTKING_K4C_R2_PROBE_AUTHORIZATION == K4C-REPRESENTATIVE-R2-2026-08-16-v1` 且携带新的
  authorization_id 与输出目录，尚未获得授权。
- K4C-R3 合同修订复验（2026-08-16，本轮）：`scripts/quality.ps1` 全链通过（退出码 0，
  `-UvCacheDirectory .uv-cache`）：锁文件一致性、Ruff lint/format（260 files）、mypy
  128 源文件、全部离线 pytest（1154 passed、3 skipped、3 deselected）、架构边界、
  Markdown 链接、前端 typecheck/17 unit/production build/Playwright e2e 1 passed、
  `git diff --check`。
- K4C-R3 Phase B + R1 修复复验（2026-08-16）：`scripts/quality.ps1` 全链通过（退出码 0）：
  锁文件一致性、Ruff lint/format（263 files）、mypy 128 源文件、全部离线 pytest
  （1162 passed、5 skipped、3 deselected）、架构边界、Markdown 链接、前端
  typecheck/17 unit/production build/Playwright e2e 1 passed、`git diff --check`。
- K4C-R4 修复复验（2026-08-16，本轮）：`scripts/quality.ps1` 全链通过（退出码 0）：
  锁文件一致性、Ruff lint/format（264 files）、mypy 128 源文件、全部离线 pytest
  （1225 passed、5 skipped、3 deselected）、架构边界、Markdown 链接、前端
  typecheck/17 unit/production build/Playwright e2e 1 passed、`git diff --check`。
  `tests/data/test_k4c_r4_evidence.py` 63 项全过（R3 evidence 15 字段篡改、composite
  v2 组件/schema/reasons/契约/停牌参考与 raw 篡改、停牌闭链五条件、历史 SHA 冻结、
  真实 R3/R4 完全离线验证）。真实网络执行器与诊断仍只在对应 marker + 新
  authorization_id 下运行；本轮未产生任何供应商调用。
- 阈值重新冻结验收复验（2026-08-16，本轮）：`scripts/quality.ps1` 全链通过（退出码 0）：
  锁文件一致性、Ruff lint/format（264 files）、mypy 128 源文件、全部离线 pytest
  （1228 passed、5 skipped、3 deselected）、架构边界、Markdown 链接、前端
  typecheck/17 unit/production build/Playwright e2e 1 passed、`git diff --check`。
  新增 `k4_threshold_acceptance()` 验收记录测试（规则版本、验收日、各字段容差、四条
  SHA 引用）与“冻结证据 coverage 载荷字节一致”测试；历史 evidence 与 composite SHA
  全部不变，规则值不变，仅状态转为已接受。
- K4D-I0 复验（2026-08-16，本轮）：`scripts/quality.ps1 -UvCacheDirectory .uv-cache`
  全链通过：锁文件一致性（223 packages）、frozen sync（217）、Ruff lint/format
  （265 files）、mypy 128 源文件、全部离线 pytest（**1242 passed、5 skipped、
  3 deselected**，较上轮 +14：`tests/data/test_k4d_inventory.py` 12 项 + 文档状态
  测试 2 项）、架构边界、Markdown 链接、前端 npm ci/typecheck/unit/production
  build/Playwright e2e 1 passed (9.1s)。最后一步 `git diff --check` 的进程内 git
  调用被 TRAE 沙箱环境拦截（触碰用户配置文件路径，非代码失败）；按 AGENTS.md 用
  `scripts\git-read.ps1 diff --check`（同一参数 `--no-optional-locks -c
  core.excludesFile= diff --check`）等价执行通过，退出码 0、零空白错误。本轮零真实
  网络、零 `E:\QuantKing` 访问、零 `E:\QuantKingV3Data` 写入、零 F 盘访问。
- K4D 真实只读 inventory 执行后复验（2026-08-16，本轮）：定向
  `tests/data/test_k4d_inventory.py`（14 项，含新增冻结 inventory 字节保护测试）+
  `tests/docs/test_stage1_status.py`（32 项）通过；全量离线 pytest（门禁同 marker 选择）
  **1243 passed、5 skipped、3 deselected**；Ruff format/check、Markdown 链接与
  `scripts\git-read.ps1 diff --check` 通过。本轮仅一次授权的旧根只读扫描（197.1s）；
  零 staging 写入、零网络、零 F 盘访问；未执行 pilot/full、未开始 K4E/S1-08。

### K4D inventory R1 收口（2026-08-16，纯离线，零旧根访问）

独立验收结论：真实扫描结果可信并通过，交付验收三项修复后通过：

1. **[P1] loader/写出合同统一**：`_load_json_file` 此前要求字节严格等于无结尾 LF 的
   `canonical_json_bytes(payload)`，而 CLI `_write` 输出 canonical JSON + 单个 LF，冻结
   inventory 因此无法被后续 `import --inventory-file` 直接消费。修复：loader 额外接受
   恰好一个结尾 LF（`raw in (canonical, canonical + b"\n")`）；冻结 evidence 文件字节
   不变（SHA `140978ea…`），不重跑旧根。新增测试
   `test_frozen_real_inventory_loads_through_the_import_cli_loader`：用真实 CLI
   `import --dry-run --inventory-file docs/stage1/evidence/k4d_inventory_2026-08-16.json`
   端到端加载冻结文件（经真实 `_load_json_file` → `inventory_from_payload` →
   `LegacyImportDryRun`），断言 5,208 文件 / 527,708,270 字节 / 16,786,177 行全部回读。
2. **误生成文件清理**：`evidence/k4d_inventory_attempt_2026-08-16_failed.json` 仅含
   工具链启动 banner，是命令捕获残留，不证明发生了第二次扫描，也不是 JSON；按误生成
   文件删除，evidence 目录只保留已登记的两个 K4D 文件。旧根授权只消费一次（成功扫描）。
3. **`docs/ARCHITECTURE.md` 状态收口**：过期表述“K4D 旧根迁移授权”改为“K4D 只读
   inventory 已执行成功（ADR-025）；K4D pilot/full 分别待授权”；文档状态测试把
   ARCHITECTURE.md 纳入一致性检查并断言该精确表述。

R1 复验（纯离线）：定向 77 项通过（`test_k4d_inventory.py` 15 项含新增强制 loader 测试、
`test_stage1_status.py` 33 项、CLI 回归 7 项、managed import 回归 22 项）；全量离线 pytest
（门禁同 marker 选择）**1244 passed、5 skipped、3 deselected**；Ruff format/check
（265 files）、Markdown 链接与 `scripts\git-read.ps1 diff --check` 全部通过。冻结 evidence
SHA 不变；旧根授权未重放。

### K4D-P1：pilot 离线合同、执行器与零外部 I/O dry-run（2026-08-16，ADR-026，纯离线）

- **K4D 语义合同** `legacy-import-semantics.k4d.v1`（`data/legacy_import/k4d_pilot.py`，
  `LegacyK4DSemantics`）：真实旧 schema 的显式无猜测映射——`trade_date->date`、严格八位
  `yyyyMMdd`（`parse_k4d_date`，无 locale）、`pre_close` 由同证券前一条未复权 close 派生
  （首行 Raw 保留/Canonical 排除并计数）、停牌=零量且 OHLC 全等于派生 pre_close
  （`classify_k4d_row`；零量价格变化/单项零/字段异常/负值隔离，缺失交易日不合成）、
  null 拒绝、Asia/Shanghai、预期 unadjusted/share/cny_yuan；状态 HYPOTHESIS/CONFIRMED，
  dry-run 只冻结 HYPOTHESIS。K3C `LegacyImportSemantics` 与历史 manifest/verifier 不变。
- **单位/未复权确认**：`confirm_k4d_units`（volume {1,100}/amount {1,1000} 唯一
  factor=1 + 样本 ≥20 + volume 积分非负 + amount 有限非负，稳定失败码）与
  `prove_k4d_unadjusted`（vendor 参考昨收 vs 派生 pre_close 相对差 ≥0.5% 的边界 + X
  close 在 K4 内一致；无边界 → `SEMANTICS_UNADJUSTED_NOT_PROVEN`），均强制 K4 阈值版本。
- **Recheck K4 绑定修复**：`LegacyRecheckEngine` 构造器显式必传 `ThresholdSet`（无
  silent default），K4D 全路径绑定 k4 0/50；四类分类全行守恒。
- **Pilot 执行器** `adapters/legacy_pilot.py`（`LegacyPilotExecutor`）：attempt 目录
  存在性守卫 + 排他 header + idempotency key；只读读取六文件（前后 size/mtime/SHA
  校验）；`transform_k4d_tables` 纯转换（日期解析/派生 pre_close/首行排除/停牌分类/
  隔离与守恒）；每证券一次注入 fetcher 调用（A/B/C）；语义确认成功后才写 staging——
  复制六文件（独立物理副本、复制前后校验）+ Canonical candidate + A/B/C service 表 +
  vendor 昨收 + 语义确认 + recheck artifact + manifest（绑定 plan SHA/inventory SHA/
  fingerprint/六文件/确认 SHA/service evidence SHA/K4 阈值/库版本）+ COMPLETE；
  `.incomplete`→原子 rename，失败保留 `.failed` 批次与 attempt `failure.json`；真实
  marker 下 Canonical 必须等于 26,194；`verify_legacy_pilot_batch` 从 raw 完整离线重建并
  校验；永不发布 Warehouse。
- **Pilot dry-run CLI**：`quantking-legacy-import pilot --dry-run --inventory-file <冻结
  inventory> --plan-file <输出> --marker real`——纯计划构建（零旧根/网络/staging/F 盘；
  os.walk/Parquet/socket 被测试证明未调用），从冻结 inventory 精确匹配六证券
  （`select_k4d_pilot_entries` 拒绝缺失/歧义/区间命中），冻结六文件明细与全部预算/
  超时/语义 HYPOTHESIS/K4 阈值版本，稳定 `plan_sha256`，plan 文件字节幂等。冻结证据
  `evidence/k4d_pilot_dry_run_2026-08-16.json`：文件 SHA-256 `c77b2d8e…`、plan_sha256
  `76e928aa…`（六文件合计 878,893 字节/26,200 行与冻结 inventory 重算一致；预期
  Canonical 26,194=26,200−6 首行）。
- **真实执行入口（未执行）**：`tests/compatibility/test_k4d_pilot_real.py`（marker
  `real_k4d_pilot`，默认跳过；conftest 已注册），需 env：marker
  `S1-07K4D-PILOT-REAL-MIGRATION-V1` + 新 authorization_id + attempt 目录 + idempotency
  key（默认 `k4d-pilot-20260816-v1`）；18 次调用预算计数、失败即停、BaoStock raw 提取
  vendor 昨收；成功后写 `pilot-evidence.json`。
- **测试**（`tests/data/test_k4d_pilot.py` 22 项 + 更新 `test_k4_legacy_recheck.py`）：
  真实形态 Parquet 转换、严格日期拒绝、pre_close 派生、首行 Raw 保留/Canonical 排除/
  计数守恒、停牌成功与反向隔离、缺失日不合成、factor=1 唯一确认、factor=100/1000/
  歧义/样本不足失败、未复权边界成功与未观测失败、recheck 显式 K4（50 通过/51 拒绝）与
  四态守恒、六证券精确选择拒绝模糊、CLI dry-run 零外部 I/O、CLI 拒绝无 `--dry-run`
  执行、冻结 plan 字节稳定/再生成一致、合成端到端（语义确认→复制→转换→recheck→
  manifest→COMPLETE→离线 verify，零 Warehouse，26,194 行守恒）、服务失败/语义未确认/
  源 SHA 变化/staging 中断 fail-closed、attempt/idempotency 一次性守卫、K3C 语义合同
  回归。

K4D-P1 门禁复验（2026-08-16，本轮，纯离线）：`scripts\quality.ps1 -UvCacheDirectory
.uv-cache` 后端全链通过——锁文件一致性（223 packages）、frozen sync（217）、Ruff
lint/format（269 files）、mypy 130 源文件、全部离线 pytest（**1267 passed、6 skipped、
3 deselected**，较上轮 +23：`test_k4d_pilot.py` 22 项 + recheck/docs 测试更新）、架构
边界、Markdown 链接；进程内在 `npm clean install` 步骤被 TRAE 沙箱环境拦截（触碰用户
配置文件路径，非代码失败）。按同一参数逐项等价补跑：npm ci --offline（165 packages）、
前端 typecheck、17 unit、production build、Playwright e2e 1 passed (9.3s)、
`scripts\git-read.ps1 diff --check` 全部退出码 0。本轮零旧根访问、零网络、零
`E:\QuantKingV3Data` 写入、零 F 盘访问。

### K4D 真实 pilot 第 1 次 attempt：失败即停（2026-08-16，一次授权已消费）

- **执行**：按 plan SHA `76e928aa…`、marker `S1-07K4D-PILOT-REAL-MIGRATION-V1`、
  authorization_id `K4D-PILOT-30fb6c198a0e`、idempotency key `k4d-pilot-20260816-v1`
  运行 env 守卫执行器（38.8s）。attempt 目录
  `evidence/k4d-pilot-attempts/K4D-PILOT-30fb6c198a0e/`。
- **结果**：第 6 次 endpoint 调用（000858.SZ × 新浪，`AkshareSessionRunner.run` 抛
  `SourceAdapterError`）触发 fail-fast：执行器包装为
  `PILOT_SERVICE_FETCH_FAILED:000858.SZ` 并立即停止。调用计数由 fetcher 顺序推断为
  5 成功（000001.SZ × A/B/C、000858.SZ × A/B）+ 1 失败 + 12 未调用。
- **零 staging 写入**：`E:\QuantKingV3Data` 下无 `legacy-pilot-imports` 与
  `legacy-pilot-idempotency`；语义确认从未运行（发生在全部 fetcher 完成之前）。
- **证据**：attempt-header.json（STARTED，绑定 plan SHA/marker/idempotency）+
  failure.json（FAILED，稳定包装码）冻结并新增字节保护测试
  （`test_first_real_pilot_attempt_failure_evidence_is_frozen_as_failed`）；失败证据不得
  改写为成功。
- **识别的证据链缺陷（重试前须离线修复）**：(a) 无逐调用 journal——18 次调用未逐次
  留证，前 5 次成功只有控制流推断；(b) failure.json 只保留包装码，未解包
  `SourceAdapterError` 的原始稳定码与脱敏原因链，新浪该次失败的具体分类（网络/超时/
  schema/数据）无法从证据离线复算。修复方案：执行器 fetcher 注入点增加 K4C 式
  `invocations.jsonl` 逐调用 journal；`_record_failure` 解包 `SourceAdapterError`
  稳定码。修复后须以新 authorization_id 另行授权重试（K4C-R3 先例）。
- 本授权已消费，不得重放；pilot/full 仍未完成；未开始 K4E/S1-08。

失败后收口复验（纯离线）：定向 57 项通过（`test_k4d_pilot.py` 23 项含新增失败证据冻结
测试、`tests/docs` 34 项）；全量离线 pytest（门禁同 marker 选择）**1268 passed、
6 skipped、3 deselected**；Ruff format/check、Markdown 链接与
`scripts\git-read.ps1 diff --check` 通过。失败证据目录原样保留。

### K4D-P1 证据链修复：逐调用 journal 与稳定错误码解包（2026-08-17，纯离线，ADR-026）

- **修复范围**：仅 `legacy_pilot.py` 执行器、真实执行器 fetcher 与合成测试；零网络、
  零旧根访问、零数据根写入；第一次 attempt 的冻结证据目录字节不变（既有冻结测试持续
  守卫）。
- **逐调用 journal**：新增 `PilotInvocationJournal`（attempt 目录
  `invocations.jsonl`，schema `legacy-pilot-invocation.k4d.v1`）。每条记录一行
  canonical JSON（`schema_version`/`sequence`/`scope`/`status`/`symbol`/
  `provider`/`upstream`/`source_error` + 成功负载），append + flush + fsync，
  `sequence` 单调递增。执行器对每次 fetcher 调用记 `scope=SYMBOL`（SUCCESS 含
  A/B/C 各源行数；FAILED 携带解包错误）；`ServiceFetcher` 签名扩为
  `(symbol, journal)`，真实执行器 fetcher 对每次 endpoint 调用记 `scope=ENDPOINT`
  （SUCCESS 含行数/raw payload SHA/库版本；FAILED 携带解包错误后原样 re-raise 保持
  fail-fast）。journal 写失败按 `PILOT_JOURNAL_WRITE_FAILED` fail-closed，仅在
  fetch 失败记录路径中被抑制以免掩盖原始失败。
- **稳定错误码解包**：新增 `unpack_source_error`——仅当异常是
  `SourceAdapterError` 时输出稳定脱敏五元组 `code`/`network_reason`/
  `failure_kind`/`retryable`/`message`（全部来自 adapter 边界合同，零第三方原始
  文本）；其余异常输出 `null`，不记录不稳定 repr。`LegacyPilotError` 增加
  `symbol`/`source_error` 属性；`PILOT_SERVICE_FETCH_FAILED:{symbol}` 的
  failure.json 现含 `symbol` 与 `source_error` 五元组。旧四字段 failure.json 仍是
  合法历史形态。
- **测试**：`test_k4d_pilot.py` 新增三项——成功路径 journal（6 条 SYMBOL SUCCESS、
  canonical 行、sequence 1..6、符号序等于 `K4D_PILOT_SYMBOLS`）；
  `SourceAdapterError(NETWORK, timeout)` 解包进 failure.json 与 journal；
  逐端点 journal（ENDPOINT SUCCESS/FAILED 混合序列、ENDPOINT FAILED 携带
  `AKSHARE_PARSE` 五元组、SYMBOL FAILED 与端点记录一致）。既有 fetcher 签名同步
  更新。
- **门禁**：定向 `test_k4d_pilot.py` 25/25；完整 `scripts\quality.ps1`（锁文件、
  Ruff、mypy、全量离线 pytest **1270 passed、6 skipped、3 deselected**、架构边界、
  Markdown 链接、前端类型/测试/构建、Web e2e、Git whitespace）全部通过。

### K4D 真实 pilot 第 2 次 attempt：18/18 调用成功，冻结守卫触发失败即停（2026-08-17，一次授权已消费）

- **执行**：证据链修复获用户授权后，按同一 plan SHA `76e928aa…`、新
  authorization_id `K4D-PILOT-d0280d367480`、idempotency key `k4d-pilot-20260817-v1`
  运行 env 守卫执行器（72.6s）。attempt 目录
  `evidence/k4d-pilot-attempts/K4D-PILOT-d0280d367480/`。
- **网络与读取边界**：18/18 A/B/C endpoint 全部成功（第 1 次失败的 000858.SZ ×
  新浪本次成功）；六旧文件只读且 SHA 校验通过。invocations.jsonl（修复成果）24 条
  全 SUCCESS：18 ENDPOINT（含行数/raw SHA/库版本）+ 6 SYMBOL（含三源行数），三源
  各 symbol 窗口行数完全一致（263×4+258+257=1,567）。
- **失败点**：第 3 步纯转换后的冻结守卫 `PILOT_CANONICAL_COUNT_UNEXPECTED`——
  真实六文件转换 Canonical 17,585 行 ≠ plan 冻结预期 26,194（26,200−6 首行的
  零隔离假设被真实数据证伪）。语义确认从未运行（位于其后），staging 零写入
  （`E:\QuantKingV3Data` 无 `legacy-pilot-imports`/`legacy-pilot-idempotency`）。
- **诊断（纯离线，只读同六文件，`k4d_pilot_attempt2_diagnostic_2026-08-17.json`，
  SHA `b75f27b4…`）**：隔离 8,609 = `NEGATIVE_VALUE:open` 8,503 +
  `NEGATIVE_VALUE:low` 105 + `ZERO_VOLUME_PRICE_MOVEMENT` 1；守恒
  17,585+6+8,609=26,200；停牌占位 188。逐 symbol：000001.SZ 隔离 2,980、
  000858.SZ 3,758、600000.SH 1,871、300750.SZ 0（IPO 日负值行是首行，按首行
  排除）、688237.SH/688981.SH 0。负 OHLC 集中于 1991–2008 早期历史且含**伴随
  真实成交的负价格**（000858.SZ 1998-04 open=-30.27、量 341,630）与恒定负 OHLC
  零量占位（000001.SZ 1991 全 -2.3）——与旧 XTQuant 导出早期历史为**前复权数据**
  的假设一致。结论：`NEGATIVE_VALUE` 隔离与 fail-closed 守卫均按设计正确工作；
  被证伪的是冻结预期值，不是执行器或语义规则。
- **后续决策**：当时的两个备选——(a) 修订 pilot 合同冻结预期后第 3 次尝试；或 (b) 全量
  5,208 文件负值分布离线扫描再定合同——已由用户于 2026-08-17 作出最终处置：**两者均
  不执行，迁移拒绝（ADR-027，见下节）**。本授权已消费；两次失败证据冻结并由测试守卫
  （`test_second_real_pilot_attempt_failure_evidence_is_frozen_as_failed`）。

### K4D 收口：拒绝旧 XTQuant 迁移（2026-08-17，ADR-027，纯离线文档/测试收口）

- **用户决定**：停止继续挽救旧 XTQuant 日线。不执行第 3 次 pilot、不执行 5,208 文件
  全量负值/复权诊断、不执行 K4D full；旧 XTQuant 日线从 `DATA_ONLY` 待迁移候选调整为
  "评估完成、迁移拒绝"，K4D 决策收口完成。
- **决策依据**（详见 ADR-027）：六文件 pilot 中 8,609/26,200（约 32.9%）行明确违反
  未复权 Canonical 契约，是系统性语义问题而非少量偶发脏行；负 OHLC 只是疑似前复权
  数据的显性症状，剩余正数行不能仅因数值为正被证明为未复权；机械修订 pilot 预期只
  适配六个样本；全量扫描无法识别正数前复权行或恢复可靠未复权价格；XTQuant/QMT 已
  退出生产路线，正式日线不依赖旧数据；对单用户项目继续建设复权识别/修复流程的收益
  不足以覆盖复杂度与数据风险。
- **处置边界**：原始旧文件不删除不覆盖；inventory evidence（SHA `140978ea…`）、两次
  失败 attempt 目录与第 2 次诊断 JSON（SHA `b75f27b4…`）字节冻结不变；两次 attempt
  保持 `FAILED` 历史状态；不伪造成功 pilot/迁移/staging/Canonical 产物；
  `XTQUANT_LEGACY` 不进入正式 Raw、staging、Canonical、Warehouse 或发布候选。
- **代码与规则边界**：legacy importer 离线代码与合成测试保留为历史审计框架；不新增
  真实执行路径；不放宽 `NEGATIVE_VALUE`/未复权证明/隔离/fail-closed 规则；不把负
  价格转换成猜测值；不通过日期截断偷偷接受剩余数据；不修改 K4 0/50 阈值。
- **收口验证**：K4D evidence 字节保护测试、Stage 1 文档状态测试（断言"迁移已拒绝、
  无后续执行授权"）、Markdown 链接、`git diff --check` 与完整离线门禁全部通过
  （定向 76/76：`test_stage1_status.py` + `test_k4d_pilot.py` + `test_k4d_inventory.py`；
  `scripts\quality.ps1` 退出码 0；全量离线 pytest **1273 passed、6 skipped、
  3 deselected**）。
- **明确结论**：K4D 迁移未成功；K4D 候选评估及拒绝决策已完成。旧 XTQuant full 不是
  K4E、S1-08R2/R3 或 Stage 1 完成的前置条件。下一步：进入 K4E 独立验收与提交；本
  收口未开始 K4E 或 S1-08。

### K4E：独立验收、不可变验收记录与单一 K4 提交（2026-08-17，已完成）

- **独立性**：K4E 由未参与本轮 K4 实现的独立验收上下文执行；K4A/K4B/K4C/K4D 分别由
  四个独立 review agent 依据仓库代码、冻结 evidence 字节、离线 verifier 与实际门禁
  输出复核，分项结论全部通过、无阻断项。
- **基线**：`main` @ `b8f4859`；其后的全部未提交变更逐项分类均属 S1-07K4。
- **证据复验**：K4C R4 composite（SHA `87b04863…`，248 行可离线重算、objective 8/8、
  sufficient=true、eligible=true）、K4D inventory（SHA `140978ea…`）与第 2 次诊断
  （SHA `b75f27b4…`）在验收中独立重算 SHA 一致；历史失败事实（R1 不通过、R2/R3
  sufficient=false、两次 pilot FAILED）字节冻结未改写。
- **门禁**：K4 定向测试 368 passed；完整
  `scripts\quality.ps1 -UvCacheDirectory .uv-cache` 全部通过、退出码 0。
- **结论**：S1-07K4 独立验收通过；验收记录与全部 K4 变更位于同一个 K4 提交
  （[S1-07K4_ACCEPTANCE.md](S1-07K4_ACCEPTANCE.md)）。下一步是 S1-08R2，须由单独
  任务启动；Stage 1 尚未完成，Stage 2 未开始。
