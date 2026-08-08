# Stage 1 收口实施与主验收交接基线

状态：本文件是 Stage 1 后续实现对话与独立主验收对话的共同导航，不是新的架构决策，也不替代
`docs/DECISIONS.md`、`docs/IMPLEMENTATION_PLAN.md`、`docs/TECHNICAL_STANDARDS.md`、
本目录任务卡和合同。发生冲突时仍按 `AGENTS.md` 的权威顺序处理。

基线审计日期：2026-08-04。审计基准为提交
`c57328904397a55ec354e5f57fc15cd4f183f2a5`。在更新本文件中的状态前，必须重新核对实际提交、
工作树、测试结果和外部副作用；不得只根据实现者自述更新。

## 1. 用途和协作方式

本文件解决两个问题：

1. 后续实现对话即使没有此前长对话上下文，也能按正确边界领取一个大小可控的任务。
2. 独立主验收对话可以依据稳定任务卡、提交和证据复核，不依赖实现对话的临时推理。

每个任务默认分为两个角色：

- **实现对话**：只完成被授权任务，运行规定检查，提交可复核的交接记录；不得自行宣称
  “完成主验收”。
- **主验收对话**：独立审查范围、代码、测试、攻击面、文档和副作用；只有实际证据满足任务卡
  才能更新主验收状态。

实现对话结束时必须提供：任务 ID、Stage、授权原文、基提交、结果提交、变更文件、迁移 head、
定向测试、完整质量门、真实 marker 执行记录、外部读写路径、产物 ID/SHA、遗留问题和工作树状态。

## 2. 当前已确认状态

| 工作项 | 状态 | 说明 |
|---|---|---|
| S1-01 | 已主验收 | 数据合同、元数据持久化与安全迁移基础 |
| S1-02 | 已主验收 | Warehouse、原子发布与显式版本查询 |
| S1-03 | 已主验收（离线/防御性） | 五类 XTQuant 真实能力仍未证明，继续归入 S1-07C |
| S1-04 | 已主验收 | 质量、跨源差异与显式 SourceOverride |
| S1-05 | 已主验收 | EOD、Worker、CLI、API 和恢复证据 |
| S1-06 | 已主验收 | 数据中心 Web 与严格读侧验证 |
| S1-07A | 已主验收 | 离线验证框架和 assessment seal 信任边界 |
| S1-07B | 已主验收 | 固定样本 v4、首个权威 ST case、reference bundle v3、按 case 前置门 |
| S1-07C | C0A/C0B/C1A-R3 已主验收；C1B 初版独立主验收未通过；**“S1-07C1B-R1 已完成独立主验收（纯离线运行就绪化）；真实运行数为 0。”** | C1A-R3：`564523eb883c15b6b52f0815f22a70b10fced5cf`，C1B-R1：`85bab3fb25840b73958c0c5fdfe2e0301605353d`；两者均不证明 XTQuant 真实数据能力或真实运行。C1（除 C1B）、C2、真实来源运行、稳定性、差异和来源结论仍未授权 |
| S1-07D | 未开始、未授权 | 旧项目 DATA_ONLY/REJECTED 审计 |
| S1-08 | 未开始、未授权 | 明确范围 Canonical 构建与 Stage 1 最终验收 |

S1-07B 完成不表示任一供应商生产可用。当前 24 个 source×dataset 单元仍为
`BLOCKED_EXTERNAL_VALIDATION`，REAL 稳定运行数为 0。Stage 1 尚未完成，Stage 2 不得开始。

2026-08-04 在上述基准上重新执行 `scripts/quality.ps1`，结果为：

- 后端：`1226 passed, 2 skipped, 3 deselected`。
- 前端：4 个测试文件、17 项单测通过；生产构建通过。
- Playwright：1 项通过。
- `uv lock --check`、Ruff、format、mypy、架构边界、Markdown 链接和
  `git diff --check` 全部通过。

该结果只证明当前离线基线健康，不证明任何真实来源能力。

## 3. 收口前必须处理的实现断点

以下是代码审计结果，不得用文档状态替代修复或验收：

1. `RealValidationRuntime` 仍是 readiness runtime。计划固定包含
   `REAL_SIGNER_NOT_INITIALIZED` 和 `PROVIDER_RUNNER_NOT_ENABLED`，执行链最终返回
   `REAL_SIGNER_UNAVAILABLE`。
2. `quantking.data.validation.runners.xtquant/baostock/akshare/tushare` 尚不存在；供应商 SDK
   也不在当前受控 Python distribution metadata 中。
3. CLI 读取旧 `ExecutionAuthorization`，但实际 runtime 参数是
   `RuntimeAuthorizationEnvelope`；旧授权对象没有进入默认执行链。
4. S1-07A evidence verifier 必须继续验证既有 v2 seal；它当前加载固定样本 v2，不能通过
   原地替换升级为 v4/v5。S1-07C 需要新增版本化证据合同并保留旧分支。
5. 当前 verifier 在结构验证后直接把 semantic/schema gate 设为 true，difference gate 又只接受
   全局 `PRODUCTION_ELIGIBLE`；这不能作为 v4 按 case REAL 证据的生产判定。
6. 当前模型不允许 REAL 身份与失败证据并存，但真实
   `CAPABILITY_UNSUPPORTED`、权限失败、限流和服务不可用必须能保存为可信 REAL attempt，且不能
   被误判为成功。
7. `SignerTrustPolicy`、assessment 持久化和 attestation summary 仍显式限制为
   SYNTHETIC/BLOCKED；生产 runtime 也没有给 DataCenter 的 UoW 注入 sealed assessment
   读侧 verifier。
8. XTQuant 生产 capability matrix 当前只有 `exchange_calendar` 可用；另外五类保持安全失败。
   只有真实证据和受控合同通过后才能逐项提升，不能一次性翻转布尔值。
9. S1-07B 正文原有三处把最终 `reference bundle v3` 写成“bundle v2”的歧义，已在本次
   H0 文档更新中统一校正；已验收 artifact ID/SHA 不变。

## 4. 推荐任务图

```text
H0  S1-03-R1 独立主验收复核（已主验收）
    ↓
C0A S1-07C 证据合同演进（纯离线，已主验收）
    ↓
C0B S1-07C 受控执行、持久化和读侧纵切（纯离线 Fake provider）
    ↓
C1A S1-07C REAL 控制链合同（纯离线，已主验收；不是真实运行）
    ↓
C1  首个经授权 XTQuant REAL 单单元运行
    ↓
C2  固定样本 v5+、来源合同和数据集晋级配置
    ↓
C3  XTQuant 六单元结论、生产适配器提升及必要的 S1-03-R2
    ↓
C4  BaoStock / AKShare / 可选 Tushare 逐单元评估
    ↓
D   S1-07D 旧数据只读审计
    ↓
E8A S1-08 明确范围与来源映射冻结
    ↓
E8B S1-08 真实 Canonical 构建和纵向证据
    ↓
E8C Stage 1 独立最终验收
```

除已主验收的 H0/C0A/C0B/C1A-R3 和 C1B-R1 纯离线运行就绪化外，C1（除 C1B）、C2 及
S1-07C/D、S1-08 的任何实现或真实运行都必须由用户按任务明确授权。前一项未主验收
时不得开始后一项；不得把一个“大任务授权”解释为后续失败重试或其他来源/数据集的授权。

## 5. 任务卡

### H0：S1-03-R1 独立主验收复核

- **Stage**：Stage 1 / S1-03-R1-Acceptance。
- **授权边界**：只读审查和既有离线测试；不访问网络、QMT、正式数据根、旧项目或 F 盘。
- **输入**：实现提交 `f55cee04747975f48f79d4891672624b83b62425`、R1 提交
  `30fe25ced01144b653eab43ad434c37859f294e1` 及当前 HEAD。
- **复核范围**：六类合成映射、能力安全失败、增量计划、Raw 信封、落地后重建、完整终态、
  loader 状态恢复、脱敏错误、不发布 `DatasetVersion`。
- **通过条件**：现有任务卡的离线/防御性范围全部满足；验收结论明确说明真实能力仍属于
  S1-07C，不得把五类能力写成可用。
- **失败处理**：记录精确阻断并建立 S1-03-R2；不得在验收任务中顺手扩大实现范围。
- **文档**：H0 已通过；S1-03 主验收状态和证据已更新，三处 bundle v2/v3 歧义已校正。

定向检查：

```powershell
uv run --offline --frozen pytest `
  tests/data/adapters/test_xtquant_adapter.py `
  tests/data/adapters/test_xtquant_mapping.py `
  tests/data/test_incremental_fetch_plan.py `
  tests/integration/data/test_xtquant_ingestion.py `
  tests/docs/test_s1_03_status.py `
  tests/test_offline_guards.py `
  -q -p no:cacheprovider
```

### C0A：S1-07C 证据合同演进（已主验收）

- **Stage**：Stage 1 / S1-07C0A。
- **主验收**：2026-08-07，接受结果提交
  `5a48909355d7a023fd404780688d070106a2522f`；证据见
  [S1-07C0A-R1 主验收证据](S1-07C0A-R1_ACCEPTANCE.md)。
- **目标**：在不破坏 v2 证据的前提下，新增可承载 v4/v5 按 case REAL 运行的版本化合同。
- **必须实现**：
  - 新的 run envelope、result attestation 和 assessment manifest 版本；verifier 按版本分派。
  - request、fingerprint、attestation 和 summary 绑定选中 case IDs、reference bundle ID/SHA、
    source×dataset contract/profile 版本。
  - REAL 成功、REAL 失败、SYNTHETIC 成功/失败的明确语义；REAL 只证明可信执行身份。
  - `CAPABILITY_UNSUPPORTED` 等可信失败可以形成 REAL/BLOCKED assessment，不携带伪造
    Canonical/Quality 成功凭证。
  - semantic、unit、coverage、quality、difference 五项 gate 均由证据推导；不得由调用方布尔值
    或无条件常量决定。
  - 数据集级晋级配置：单个 AUTHORITY_APPROVED case 只允许执行，不自动证明整个
    source×dataset 为 `PRODUCTION_READY`。
- **兼容性**：既有 v2 seal、Fake signer 和 S1-07A 证据继续按原字节验证；不得重签、迁移或
  修改失败产物。
- **禁止项**：不导入或安装供应商 SDK，不访问网络/QMT/正式根，不产生 REAL 身份。
- **验收重点**：降级/混淆攻击、case 缩减、bundle 替换、重新自签、版本漂移、失败冒充成功、
  v2 回归。

### C0B：受控执行、持久化和 DataCenter 纵切

- **Stage**：Stage 1 / S1-07C0B。
- **状态**：已完成独立主验收（纯离线 Fake provider），证据见
  [S1-07C0B-R1 主验收证据](S1-07C0B-R1_ACCEPTANCE.md)。
- **前置**：C0A 主验收；C0B 纯离线实现和独立主验收均已完成。
- **目标**：使用 Fake provider 打通未来 REAL 运行的完整控制流，但生产默认仍失败关闭。
- **必须实现**：
  - 单一、规范的 `RuntimeAuthorizationEnvelope` 文件加载与验证；删除或封闭旧授权旁路。
  - 计划先完成安全路径、包版本、case、bundle、合同和 signer/runner readiness 预检，再领取
    一次性授权；领取后才允许固定模块 import 和输出创建。
  - 受控 REAL signer 接口及生产 key-id/profile allowlist；密钥不入 Git、日志或 artifact。
    Fake key 永久是 SYNTHETIC。
  - runner 模块不直接导入供应商 SDK；SDK 只能在 `data/adapters` 内延迟加载。
  - runner 成功、失败和崩溃均生成不可覆盖证据；授权消费后失败不得删除 claim。
  - assessment seal 持久化允许 verifier 推导的 REAL/BLOCKED 或 REAL/READY，同时继续拒绝手工
    DTO、直接 SQL 和调用方 trust profile。
  - 生产 runtime 给 DataCenter 的 UoW 注入相同 sealed read verifier；读取时再次深验 seal。
- **迁移**：若新增机器可查询字段或状态，必须建立 Alembic 升级、临时副本测试、历史隔离和
  必要恢复说明；不得覆写 `0012`。
- **禁止项**：不启用真实 runner，不读取正式供应商，不写正式数据根。

### C1A：REAL 控制链合同（已主验收，纯离线）

- **Stage**：Stage 1 / S1-07C1A-R3。
- **主验收**：结果提交 `564523eb883c15b6b52f0815f22a70b10fced5cf`；证据见
  [S1-07C1A-R3 主验收证据](S1-07C1A-R3_ACCEPTANCE.md)。
- **范围**：固定单元的 REAL authorization、claim、capability-blocked runner、evidence、
  seal、persistence 与 DataCenter 读侧离线控制链。
- **结论边界**：只证明合同在离线攻击下失败关闭；不证明 XTQuant 真实数据能力、网络/QMT、
  历史 ST 语义或任何真实运行，也不授权 C1/C1B/C2。

### C1：首个经授权 XTQuant REAL 单单元运行

- **Stage**：Stage 1 / S1-07C1。
- **候选 scope**：`XTQUANT × trading_status × st_warning_603377_sh`。这是当前唯一
  AUTHORITY_APPROVED case，只适合作为真实控制链薄切片。
- **逐次授权**：真实 XTQuant/QMT/网络、精确 output root、source、dataset、case、包版本、
  有效期和一次性 authorization ID。失败重试必须重新授权。
- **目标**：证明 plan → authorization → claim → adapter → Raw → Canonical/失败 → quality →
  attestation → seal → persistence → DataCenter 的真实边界。
- **允许结论**：READY、真实质量/合同阻断或 `CAPABILITY_UNSUPPORTED` 都是有效发现；不得预设
  供应商支持，也不得为了通过而构造历史状态。
- **禁止项**：不调用交易/下单模块，不扩大到其他 case，不自动进行第二或第三次稳定运行。

### C2：固定样本 v5+、来源合同和晋级配置

- **Stage**：Stage 1 / S1-07C2。
- **目标**：为后续六类真实评估准备最小但充分的权威 case 和来源语义合同。
- **版本纪律**：v1～v4 和 authority bundle v3 全部冻结；任何新增或修订创建 v5+、新 bundle
  ID 和新受控 release。
- **建议的数据集配置**：
  - `security_master`：正常、上市/IPO、退市、point-in-time。
  - `exchange_calendar`：三交易所正常开闭市及明确假日/缺失语义。
  - `daily_bar_unadjusted`：正常、单位、缺失/不支持及必要边界事件。
  - `adjustment_factor`：公司行动、因子公式、基准日和单位。
  - `trading_status`：正常、ST/*ST、停复牌、退市整理、涨跌停和 point-in-time。
  - `benchmark_index_daily`：沪深基准、昨收及可空 volume/amount 单位。
- **决策要求**：上述配置是建议，不是已接受决策。实现前由用户确认每个数据集晋级到
  `PRODUCTION_READY` 所需的最小 purpose 集。
- **外部材料**：网络读取、官方材料下载、正式根写入和用户 review decision 分别授权；外部
  大段正文/PDF 不进入 Git。

### C3：XTQuant 六单元结论与生产能力提升

- **Stage**：Stage 1 / S1-07C3；必要时同时建立 S1-03-R2。
- **目标**：对六个 XTQuant 单元逐项形成真实、可复查结论；只把有完整合同、质量、差异和
  稳定性证据的单元接入生产 Gateway。
- **规则**：
  - 每个真实 invocation 单独授权；三次稳定运行必须是独立 run/invocation/session/time。
  - 支持结论必须冻结 SDK/package、adapter、schema、规则、阈值、sample、config 和代码版本。
  - 不支持结论保存 REAL/BLOCKED 证据和稳定原因，不得保持“未验证”措辞伪装已完成。
  - production capability 逐单元提升；不得一次性把五个 `available=False` 全改为 true。
- **S1-03 闭环**：若 H0 未通过或真实证据要求生产适配器修复，完成 S1-03-R2 后必须独立主验收，
  再继续备用源评估。
- **阻断决策**：若六类必需数据中存在没有可生产来源的单元，停止并报告；不得通过缩小文档措辞
  宣称 Stage 1 可完成。

### C4：备用源真实矩阵

- **Stage**：Stage 1 / S1-07C4，按 source×dataset 拆成独立子任务。
- **优先级**：BaoStock 核心日线候选 → AKShare 补充/交叉校验 → 可选 Tushare。
- **依赖安装**：新增 SDK 前说明版本、许可证、Python 3.12/Windows 支持、锁文件、离线缓存、
  凭据和替代方案；安装/下载需单独授权。
- **结论**：每个实际评估单元产生 READY 或 REAL 证据支持的 BLOCKED/UNSUPPORTED 结论。
  fallback 永远 `auto_switch=False`，使用仍需显式 DifferenceReport 和 SourceOverride。
- **待用户确认的收口口径**：建议要求六类必需数据各有至少一条可生产来源路径；已选择评估的
  source×dataset 必须有真实结论。Tushare 作为可选来源可由用户明确豁免，但不得写成已验证。

### D：S1-07D 旧数据只读审计

- **Stage**：Stage 1 / S1-07D。
- **逐次授权**：读取 `E:\QuantKing`、创建隔离副本、写审计 evidence 分别明确授权。
- **候选组**：
  - 根目录 1 个约 978,944 字节数据库。
  - `.worktrees/v3-stage-0` 下 1 个约 53,248 字节数据库。
  - `data/kline/1d` 下 5,208 个、合计约 527,708,270 字节 Parquet。
- **方法**：先创建不可变 inventory/SHA；数据库只用只读 immutable 模式检查 schema 和统计，
  不加载扩展、不执行旧代码；Parquet 检查 schema、主键、排序、日期、代码、缺失、NaN/Inf、
  复权/单位/来源和覆盖。
- **结论**：每组只能是 `DATA_ONLY` 或 `REJECTED`，并附理由和证据。全部 REJECTED 是合法结果；
  Stage 1 不要求强行迁移旧数据。
- **禁止项**：不整体复制旧项目，不把旧数据标成 XTQuant，不直接发布 Canonical，不修改旧文件。

### E8A：S1-08 范围与来源映射冻结

- **Stage**：Stage 1 / S1-08A。
- **前置**：S1-02～S1-07 全部主验收；用户明确授权 S1-08A。
- **输出**：六类数据集各自的精确来源、代码/交易所范围、起止日期、预期覆盖、允许缺口、base
  version、质量规则、差异阈值、输出根和幂等键；不得使用 `latest`。
- **通过条件**：六类必需数据都有已验收的输入路径；全量历史证券要求在批准范围内进入底层，
  或有逐项明确缺口报告。

### E8B：真实 Canonical 构建与纵向证据

- **Stage**：Stage 1 / S1-08B。
- **逐次授权**：真实网络/QMT、正式数据根写入和失败重试分别授权；默认不写 F 盘。
- **执行路径**：只通过既有 EOD 应用服务、Worker、质量门和发布协调器，不直接写 Warehouse
  或伪造 SQLite 状态。
- **完成条件**：六类明确范围版本均可查询，质量通过，失败隔离，重复命令幂等，小样本差异可
  复现，DataCenter 展示来源、覆盖、质量、差异和失败。
- **失败纪律**：失败批次/version/artifact 保留原样；修复后使用新 job/batch/version ID 和
  新授权。

### E8C：Stage 1 独立最终验收

- **Stage**：Stage 1 / Final Acceptance。
- **角色**：必须由独立验收对话执行，不得只复述 E8B 实现者结论。
- **验收矩阵**：逐项复核 `docs/stage1/ACCEPTANCE.md` 五个阶段完成条件、S1-02～S1-08 任务
  证据、六类版本血缘、真实授权记录、失败产物、Web 展示和完整质量门。
- **状态更新**：只有全部通过后才能更新 `IMPLEMENTATION_PLAN.md`、`ROADMAP.md`、Stage 1
  README/任务卡/验收矩阵并创建独立最终验收报告。否则 Stage 1 保持进行中。
- **禁止项**：不得同时开始 Stage 2，不得用“明确范围”掩盖未批准的范围缩减。

## 6. 授权矩阵

| 动作 | 默认状态 | 授权粒度 |
|---|---|---|
| 仓库内纯离线代码/测试 | Stage 1 总体已授权，但 C1 及后续任务仍按本任务卡重新确认 | 一个任务 |
| 真实 XTQuant/QMT/网络 | 禁止 | source×dataset×case×invocation |
| BaoStock/AKShare/Tushare 网络或 SDK 安装 | 禁止 | 包安装或一次真实 invocation |
| 写 `E:\QuantKingV3Data` | 禁止 | 精确 output root 和任务 |
| 读 `E:\QuantKing` | 禁止 | 精确审计任务和路径 |
| 写 `F:\QuantKingV3Backups` | 禁止 | 精确备份/恢复任务 |
| 失败后的真实重试 | 禁止复用原授权 | 新 invocation、新 authorization ID |
| Stage 2 及以后 | 未授权 | 新 Stage 决策 |

账号、密码、token、授权签名或 signer 私钥不得写入对话、Git、日志或证据。计划和失败消息只保存
稳定、脱敏事实。

## 7. 主验收协议

后续实现对话提交验收时，主验收对话按以下顺序复核：

1. 读取 `AGENTS.md` 和本任务所属权威文档；确认 task ID、Stage 和授权原文。
2. 检查基提交、结果提交、工作树和变更范围；拒绝混入下一任务或 Stage 2 能力。
3. 逐文件审查领域边界、供应商导入位置、失败语义、版本/血缘和不可变性。
4. 先运行任务定向测试，再运行新增攻击回归；必要时先写红测复现缺口。
5. 运行完整 `scripts/quality.ps1`；真实 marker 只在对应授权仍有效时单独执行。
6. 复核 migration 唯一 head、新库升级、旧 head 升级和必要的恢复/降级说明。
7. 复核实际外部副作用和 evidence SHA；实现者未披露的外部访问是阻断问题。
8. 只有无阻断问题时记录主验收提交；否则建立明确 R 任务，保持原状态。

完整离线门禁仍以 `scripts/quality.ps1` 为唯一组合入口。定向命令不能替代完整门禁，完整门禁也
不能替代真实来源证据。

## 8. 后续状态记录模板

每次主验收后在相应独立 acceptance 文档记录，必要时同步本节；不得覆盖历史行。

| 日期 | 任务 | 基提交 | 结果提交 | 主验收 | REAL 运行 | 说明 |
|---|---|---|---|---|---:|---|
| 2026-08-04 | 收口基线审计 | `c573289` | - | 非任务主验收 | 0 | 离线质量门全绿；建立后续交接基线 |
| 2026-08-05 | H0：S1-03-R1 独立主验收复核 | `c573289`（复核 `f55cee0`、`30fe25c`） | `0572848d512f3a0117ff8a0194b76513403ca076` | 已主验收（离线/防御性） | 0 | 定向 `116 passed`；完整 `quality.ps1` 退出码 0；五类真实能力继续归入 S1-07C |
| 2026-08-07 | S1-07C0A-R1 独立主验收 | `07fa7d1` | `5a48909` | 已主验收（纯离线） | 0 | v3 证据合同；不证明 REAL 来源能力，不授权 C1 |
| 2026-08-07 | S1-07C0B-R1 独立主验收 | `20c7aab` | `20c7aaba4de4a03b58ea148c4f56e8aec3eb2092` | 已主验收（纯离线 Fake provider） | 0 | 受控 Fake 执行、seal 持久化和正式 DataCenter 重验；不证明 REAL 来源能力，不授权 C1 |
| 2026-08-08 | S1-07C1A-R3 独立主验收 | `a9b7207` | `564523eb883c15b6b52f0815f22a70b10fced5cf` | 已主验收（纯离线 REAL 控制链） | 0 | authorization/claim/runner/reader 合同；不证明 XTQuant 真实数据能力，不启动 C1/C1B/C2 |
| 2026-08-08 | S1-07C1B 初版独立主验收 | `23a8212` | `72b10fd` | 未通过 | 0 | 计划、完整 authorization 和 signer 私钥可位于仓库内；建立 R1，不构成真实运行 |
| 2026-08-08 | S1-07C1B-R1 独立主验收 | `72b10fd` | `85bab3fb25840b73958c0c5fdfe2e0301605353d` | 已主验收（纯离线运行就绪化） | 0 | 仓库外 plan/auth/key 边界；不证明 XTQuant 真实数据能力或真实运行 |

## 9. 下一对话的默认入口

C0A/C0B/C1A-R3 已完成纯离线独立主验收；C1B 初版独立主验收未通过；**“S1-07C1B-R1
已完成独立主验收（纯离线运行就绪化）；真实运行数为 0。”**
下一个任务仍需用户按任务明确授权领取；本次验收不自动开始 C1（除 C1B）、C2、任何真实运行、S1-07D、
S1-08 或 Stage 2，也不授予真实外部访问权限。任何对话不得根据
本文件推定这些工作已经获得授权。
