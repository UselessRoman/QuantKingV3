# S1-07B 主验收证据

状态：S1-07B 已于 2026-08-04 在提交 `d60559601efd61de15b41ffa86b4f2f730a69d5e`
完成主验收。权威核心样本 v4、首个 AUTHORITY_APPROVED
case（603377.SH ST）、reference bundle v3 证据链和按 case 放行运行时已定版。17 个
purpose 仍是覆盖 backlog，不是 S1-07B 全局阻断门。

## 主验收结论

独立主验收以提交 `d60559601efd61de15b41ffa86b4f2f730a69d5e` 为基准，确认受控
authority bundle release、伪造 bundle 失败关闭、不可前视时间关系、统一
`RealValidationRuntime` 执行入口、claim/import/output 前阻断，以及失败 attempt 和并发冲突
取证均符合合同。正式 bundle 复验后唯一批准 case 为 `st_warning_603377_sh`。

独立定向验收为 `136 passed`；完整 `scripts/quality.ps1` 退出码为 0，后端为
`1223 passed, 2 skipped, 3 deselected`，前端 17 项单测、生产构建和 Playwright 全部通过；
Ruff、mypy、架构边界、Markdown、whitespace 通过；Alembic 唯一 head 为
`20260729_0012`，验收时工作树干净。

该主验收不改变后续边界：其余 16 个 purpose 仍为 backlog，24 个 source×dataset 单元仍为
`BLOCKED_EXTERNAL_VALIDATION`，REAL 稳定运行数仍为 0；S1-07C/D、S1-08 尚未开始，Stage 1
尚未完成。

## 权威范围校正

顶层 [实施计划](../IMPLEMENTATION_PLAN.md) 要求建立固定样本并在后续真实运行中评价主源和
备用源，但没有要求 S1-07B 在真实运行前一次性收齐 17 类事件。`UNIT_SEMANTICS`、
`ADJUSTMENT_FORMULA`、`MISSING_OR_UNSUPPORTED`、稳定运行和跨源差异等结论天然依赖真实
供应商响应，属于 S1-07C。

因此，17 个 purpose 作为覆盖 backlog 和逐 case 阻断条件，而不是 S1-07B 的全局完成门。
不得删除这些 purpose，也不得把缺失 purpose 伪装成完成。S1-07B 完成只表示：权威样本合同、
至少一个获批准的权威 case、reference 证据链及按 case 放行机制已经定版。

Stage 1 最终验收仍被 S1-07C/D、真实来源验证和剩余 source×dataset 单元阻断。

## 范围与基线

- Stage：Stage 1 / S1-07B-FINAL。
- 父提交：`c498a7f5ab5c04b63ab2b6a0ffb762e1d5dadf4a`。
- S1-07B 基线：`1b818018e9b8ca9f56353b0e78b37008912ba86d`。
- S1-07B-R1 基线：`310b2758cdb4595fc3e89891c7ca0c30a927447e`。
- 本任务在 R1 基础上完成：v4 权威核心样本定版、603377.SH ST 权威 case、reference
  bundle v2 证据链、时间语义修正和运行时按 case 放行。
- P2B（authority-discovery）提供 document-specific CNINFO locator 证据；P2D/P2E 提供
  接口/材料路径证据；P2F（authority-material-import）提供首个权威事实材料。它们不是白做。
- 没有联网、QMT/MiniQMT、真实 XTQuant 或备用源调用；没有读取正式数据根、旧项目或备份根
  （授权读取的已封存 P2B/P2F evidence 除外）。
- 没有开始 S1-07C/D、S1-08 或 Stage 2。

## 首次红测

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/data/validation/test_s1_07b_final.py -q -p no:cacheprovider --tb=no
```

原 FINAL 实现复核后的首次红测结果为 `5 failed, 83 passed`。根因包括：正式 bundle builder
按 `sample.cases[0]` 的位置误取 `NORMAL` purpose，writer 未在原子完成前把 draft 绑定受控
sample 并深验，以及三份 Stage 1 文档仍残留 R1 阻断状态。原先生成的
`st-603377-authority-v1` 因 fact/binding purpose 与 ST case 不一致，被正式 verifier 以
`AUTHORITY_BUNDLE_FACT_MISMATCH` 安全拒绝。v2 修正 purpose 后又在复核中发现 identity
statement 合并了公司名、但 paragraph SHA 只对应页首段落，因此也未作为最终运行时输入。
v1/v2 目录均原样保留、未覆盖；v3 将证券与公司身份结构化，并使 identity statement 与页首
段落 SHA 精确对应。最终 verifier 对 v2 稳定返回 `AUTHORITY_BUNDLE_MANIFEST_MISMATCH`。

修复后全部转绿（见下文验证与副作用）。

## 固定样本版本链

| version | sample SHA-256 | release/规范字节 SHA-256 | 状态 |
|---|---|---|---|
| s1-07.fixed-sample.v1 | `bc40271a632e0f8b4bae493806bf3d316897857a1102ed01b70f2bb46c632bee` | `aafb03db4a3845aa710848dc1f12542350afbb57d6edae466cf0978b5c0bc249` | 冻结、不改写 |
| s1-07.fixed-sample.v2 | `03d3d6c1e898b0746b7c5ab3911917a600fa3975f802c1d414fd0b6196c80dfb` | `2b692c631018c1e600680af329d3936dd98267d9d82ffdbcbe854b3ca8574cb8` | 冻结、不改写 |
| s1-07.fixed-sample.v3 | `5b5bf202e17181dfcbd9159f261dc9b4c636fb7641a3786523b42f952c6f3200` | `52bc879ed5f42ea6a8fe0d6b489d4d9c1153e3568379d614539b65ca207e72eb` | 冻结、不改写 |
| s1-07.fixed-sample.v4 | `859726293e8ed2e77663b6a4168b29a2cfd48070236ec5b8815ad2ee3b5904d7` | `48b054df659423c93b198aa63660892ba221db9c2d3afd008715ed8bca3c6034` | `VALIDATION_READY_WITH_BLOCKED_CASES` |

v4 直接绑定 v3 sample SHA。v1/v2/v3 的字节、SHA、release 和资格不变。对 v4 release 的
eligibility、缺口、previous SHA、用途集合、版本或 sample SHA 作任何修改都会偏离仓库受控
规范字节并被拒绝。后续新增材料必须创建 v5+，不得修改 v4。

## v4 全部 case 及资格状态

v4 原样继承 v3 的 11 个 framework case（保持 `FRAMEWORK_ONLY`），并新增 1 个 ST 权威
case（`AUTHORITY_APPROVED`）。全局 release 资格为 `VALIDATION_READY_WITH_BLOCKED_CASES`，
不使用容易被理解为 17 个 purpose 全部闭合的全局 `PRODUCTION_ELIGIBLE`。

资格层级：

| 资格 | 语义 |
|---|---|
| `FRAMEWORK_ONLY` | 仅证明请求/schema/unit 框架，不证明证券事件事实；不可通过 runtime gate |
| `AUTHORITY_APPROVED` | 已获权威材料批准且有完整 reference binding；可通过 runtime sample/reference gate |
| `VALIDATION_READY` | 已通过框架验证，等待权威材料批准 |
| `BLOCKED_REFERENCE` | reference 证据不完整或被拒绝；不可通过 runtime gate |

| case_id | dataset | code/exchange | date range | purpose | 资格 |
|---|---|---|---|---|---|
| adjustment_normal_sh | adjustment_factor | 600000.SH / SH | 2024-01-02..2024-01-05 | NORMAL | FRAMEWORK_ONLY |
| benchmark_sh_composite | benchmark_index_daily | 000001.SH / SH | 2024-01-02..2024-01-05 | BENCHMARK | FRAMEWORK_ONLY |
| benchmark_sz_component | benchmark_index_daily | 399001.SZ / SZ | 2024-01-02..2024-01-05 | BENCHMARK | FRAMEWORK_ONLY |
| calendar_bj | exchange_calendar | BJ | 2024-01-02..2024-01-05 | NORMAL | FRAMEWORK_ONLY |
| calendar_sh | exchange_calendar | SH | 2024-01-02..2024-01-05 | NORMAL | FRAMEWORK_ONLY |
| calendar_sz | exchange_calendar | SZ | 2024-01-02..2024-01-05 | NORMAL | FRAMEWORK_ONLY |
| normal_bj_equity | daily_bar_unadjusted | 430047.BJ / BJ | 2024-01-02..2024-01-05 | NORMAL | FRAMEWORK_ONLY |
| normal_sh_equity | daily_bar_unadjusted | 600000.SH / SH | 2024-01-02..2024-01-05 | NORMAL | FRAMEWORK_ONLY |
| normal_sz_equity | daily_bar_unadjusted | 000001.SZ / SZ | 2024-01-02..2024-01-05 | NORMAL | FRAMEWORK_ONLY |
| security_master_normal_sz | security_master | 000001.SZ / SZ | 2024-01-02..2024-01-05 | NORMAL | FRAMEWORK_ONLY |
| trading_status_normal_sh | trading_status | 600000.SH / SH | 2024-01-02..2024-01-05 | NORMAL | FRAMEWORK_ONLY |
| st_warning_603377_sh | trading_status | 603377.SH / SH | 2026-03-23..2026-07-31 | ST | AUTHORITY_APPROVED |

## 603377.SH ST 权威事实

### 文档身份

- 证券代码：603377.SH
- 证券简称：ST 东时
- 公司：东方时尚驾驶学校股份有限公司
- 公告编号：临 2026-110
- 材料 SHA-256：`f3deec88dbd796c44535ecf72965c7f2103aca2ee663125b6499a427545c98e0`
- CNINFO document-specific locator：
  `https://www.cninfo.com.cn/new/disclosure/detail?stockCode=603377&orgId=9900023616&announcementId=1225449700&announcementTime=2026-07-31`

### 四项批准事实及时间语义

| fact_id | fact_nature | effective_date | document_date | publication_date | known_from_date | usable_for_decisions_after | 关键字段 |
|---|---|---|---|---|---|---|---|
| st-document-identity-001 | IDENTITY | - | 2026-07-30 | 2026-07-31 | 2026-07-31 | 2026-07-31 market close | 文档身份 |
| st-status-20260323 | RETROSPECTIVE_CONFIRMATION | 2026-03-23 | 2026-07-30 | 2026-07-31 | 2026-07-31 | 2026-07-31 market close | other_risk_warning=true（规则 9.8.1 第一款第（七）项） |
| st-status-20260418 | RETROSPECTIVE_CONFIRMATION | 2026-04-18 | 2026-07-30 | 2026-07-31 | 2026-07-31 | 2026-07-31 market close | other_risk_warning=true（规则 9.8.1 第一款第（六）项） |
| st-continues-20260730 | CONTINUATION | - | 2026-07-30 | 2026-07-31 | 2026-07-31 | 2026-07-31 market close | other_risk_warning_continues=true |

时间语义约束：

- `effective_date`（2026-03-23、2026-04-18）是事件生效日，证据性质为回溯确认
  （`RETROSPECTIVE_CONFIRMATION`），不得表达该事实在生效日当时已被本系统知晓。
- `document_date`（2026-07-30）只作为材料正文日期，不是 point-in-time 可用日期。
- `publication_date`/`known_from_date`（2026-07-31）是材料公开/可知日。
- 没有可靠的日内发布时间证据时，不得声称该材料在 2026-07-31 开盘前可用。日频研究的保守
  可用边界为 `usable_for_decisions_after = 2026-07-31 market close`（等价下一交易日可用）。
- 不得在 publication date 之前用于 point-in-time 查询。

### 明确未批准的推导

以下 purpose 仍不得从该 PDF 推导：`STAR_ST`、`DELISTING`、`DELISTING_PERIOD`、
`SUSPENSION`、`RESUMPTION`、`LIMIT_UP`、`LIMIT_DOWN`、`IPO`、`LISTING`、
`POINT_IN_TIME` 当时已知性、日线 OHLC/pre_close/volume/amount、供应商能力、字段单位、
完整历史覆盖。

## Reference bundle v2

### 路径与凭证

- 路径：`E:\QuantKingV3Data\validation\s1-07b\reference-bundles\st-603377-authority-v3`
- Bundle ID：`st-603377-authority-v3`
- Manifest SHA-256：`94ac8da21586a13803058b80344ff1b8dd63633c091e90b8cba832b5ef6a0bea`
- Reference inventory SHA-256：`a40c1ee28ff545aca29fd292d18bfc241f95ff02eadbdee6de5083731ba1c07c`
- Sample version：`s1-07.fixed-sample.v4`
- LOCAL_OWNER decision date：`2026-08-04`

### 外部证据复验

构建前复验已封存 P2B/P2F evidence：

| 证据 | 预期 SHA | 复验结果 |
|---|---|---|
| P2B manifest | `0ae4809084bac61526d99d0576081cee5741b6b248e23015a2d9592e1a587df7` | 通过 |
| P2F manifest | `15fc9f0b122b68dfdb11d9fa8e15870d9b65d7286e77f9a971fe2912fe8c4ed9` | 通过 |
| P2F inventory | `c61db67b098936d4308b53985ca23780ac42b0957faed207b59cf3c1168ecdd6` | 通过 |
| PDF material | `f3deec88dbd796c44535ecf72965c7f2103aca2ee663125b6499a427545c98e0` | 通过 |
| P2F 逐文件 SHA（12 files） | manifest files 列表 | 全部通过 |

不依赖 Downloads 中的原始文件；使用已封存 P2F evidence。不修改 P2B、P2F 或任何既有证据。
不把 PDF、网页或外部证据目录纳入 Git。Git 只记录规范化事实、locator、材料 SHA、bundle SHA、
版本和合同。

### Bundle 文件集合

```
st-603377-authority-v3/
├── references/
│   └── cninfo-603377-1225449700.json
├── facts.json
├── bindings.json
├── review_decisions.json
├── manifest.json
├── manifest.sha256
└── COMPLETE
```

### 合同

- `ReferenceDocument` 绑定 document-specific CNINFO locator、公告编号、证券代码/简称、公司、
  PDF SHA、P2B/P2F manifest SHA，并分别记录 `document_date`、`publication_date`、
  `known_from_date`。
- `ReferenceFact` 分别表达文档身份、两个回溯有效事实和发布时继续实施事实。
- `CaseFactBinding` 精确绑定 case、fact 和 purpose。
- `ReviewDecision` 记录 `LOCAL_OWNER`、`APPROVED`、批准范围及批准语句的规范 SHA。
- 生产 verifier 内部加载 Git 控制、字节 SHA 固定的 `authority_bundle.release.v1.json`；该
  release 精确冻结 bundle manifest/inventory、v4 sample 血缘、CNINFO locator、公告标识、
  P2B/P2F/PDF SHA、四项 fact ID/段落 SHA/页码/时间字段和批准声明 SHA。调用方不能注入、
  替换或自行签发 trust policy。
- self-signed manifest、手工 `LOCAL_OWNER/APPROVED` 或 public writer 只证明结构自洽；只有
  与受控 release 完整相等的 bundle 才能产生 `AUTHORITY_APPROVED`。
- 每个 fact 与 binding 的 purpose 必须精确等于被绑定 case 的 `ST`；正式 writer 在创建 final
  目录前以受控 v4 sample 验证该关系，并在 `.incomplete` 内运行完整 verifier。
- 正式 builder 还逐字段复验 P2F 四条 sealed candidate 的 purpose、证券、公司、公告、日期、
  locator、材料 SHA、页码和段落 SHA；只复验整个 P2F 文件 SHA 不足以建立事实 binding。
- 该批准只证明用户复核了固定样本事实，不得被当成 REAL provider signer。不生成或伪装任何
  REAL signer。reviewer decision 与 REAL provider identity 是两个独立概念。
- bundle 使用 `.incomplete` → 结构验证 → manifest/SHA/COMPLETE → 原子完成；失败保留唯一
  attempt 目录、原输入 inventory 和脱敏 `failure.json`，不删除或覆盖取证。
- 已存在同 bundle ID 时，完全相同才允许幂等返回；内容冲突必须拒绝，不得覆盖。
- 拒绝 symlink、junction、reparse point、路径逃逸、额外文件和证据篡改。
- 缺文件、多文件、SHA 篡改、locator 替换、事实替换、decision 替换均拒绝。

## 运行时按 Case 放行

### 放行规则

运行计划显式冻结：sample version/SHA、case ID 集合、source、dataset、request scope、
reference bundle ID/SHA、adapter/package/code/schema/rule/threshold/config version、
output root、authorization ID。

- 所有被选择 case 必须属于对应 dataset。
- 所有被选择 case 必须具有完整、已验证且获批准的 reference binding。
- 任一选中 case 为 `FRAMEWORK_ONLY`、`BLOCKED_REFERENCE` 或证据不完整，整次计划失败关闭。
- 选择新增 ST 权威 case 时，reference bundle 完整且其他 preflight 条件满足，允许通过
  sample/reference 资格门。这只表示"可以进入 S1-07C 的 REAL runtime 前置检查"，不表示
  供应商包、REAL signer、QMT 或网络已可用。
- 当前 provider package 仍为 `unavailable` 时，继续返回既有稳定阻断错误。
- CLI、脚本和应用组合统一调用 `RealValidationRuntime`；旧 v2 sample 与 CLI 直接 runner import
  路径已删除。任一 blocking reason 在 authorization claim、runner import 和 output/session
  创建前拒绝。
- 不执行真实 runner，不消费任何真实网络/QMT 授权。
- 未选中的缺失 purpose 不阻断已获批准 case。缺失 purpose 仍保留在 coverage backlog，
  并阻断选择它们的运行计划。
- 禁止调用方通过自定义布尔值、伪造 reviewer、子类属性或手工 DTO 提升 case 资格。

### Case 级 runtime 放行/阻断矩阵

| 选中 case | reference bundle | 预期结果 |
|---|---|---|
| st_warning_603377_sh（AUTHORITY_APPROVED） | 完整且已验证 | 通过 sample/reference gate（仍受 package/signer 阻断） |
| calendar_sh（FRAMEWORK_ONLY） | 无 | 失败关闭：`CASE_NOT_AUTHORITY_APPROVED` |
| st_warning_603377_sh + calendar_sh（混合） | 部分 | 失败关闭：`CASE_NOT_AUTHORITY_APPROVED` |
| 未选择任何缺失 purpose | - | 不阻断已批准 case |
| 选择缺失 purpose 的 case | 无 reference | 失败关闭 |
| 伪造全局 production-ready / case 状态 / reviewer | - | 拒绝 |

### 版本合同

| 凭证 | 冻结值/来源 |
|---|---|
| adapter | `xtquant-readonly.s1-07b.v1` / `baostock-readonly.s1-07b.v1` / `akshare-readonly.s1-07b.v1` / `tushare-readonly.s1-07b.v1` |
| provider package | 仅由 `importlib.metadata` 本机只读探针取得精确版本；缺失时为 `unavailable` 并阻断 |
| code | `s1-07b.runtime.v1` |
| schema | 各 dataset 的受控 Canonical descriptor version |
| rule | `quality-rules.v1` |
| threshold | `comparison-thresholds.v1` |
| sample | `s1-07.fixed-sample.v4` / `859726293e8ed2e77663b6a4168b29a2cfd48070236ec5b8815ad2ee3b5904d7` |
| config | `real-validation-config.v1` |

本次本机只读 metadata 结果：XTQuant、BaoStock、AKShare、Tushare 均为 `unavailable`。
未安装、升级或导入任何供应商包。

## 17 purpose backlog 状态

| purpose | 状态 | 说明 |
|---|---|---|
| ST | 已批准 | 603377.SH ST 权威 case 已获 AUTHORITY_APPROVED |
| NORMAL | 缺失 | 继承 framework case，未获权威材料 |
| BENCHMARK | 缺失 | 继承 framework case，未获权威材料 |
| ADJUSTMENT_FORMULA | 缺失 | 依赖供应商正式字段/算法合同 |
| UNIT_SEMANTICS | 缺失 | 依赖供应商正式字段合同 |
| MISSING_OR_UNSUPPORTED | 缺失 | 依赖供应商正式能力合同及真实响应 |
| POINT_IN_TIME | 缺失 | 依赖真实 point-in-time 验证 |
| IPO | 缺失 | 未取得权威材料 |
| LISTING | 缺失 | 未取得权威材料 |
| DELISTING | 缺失 | 未取得权威材料 |
| DELISTING_PERIOD | 缺失 | 未取得权威材料 |
| STAR_ST | 缺失 | 未取得权威材料 |
| SUSPENSION | 缺失 | 未取得权威材料 |
| RESUMPTION | 缺失 | 未取得权威材料 |
| LIMIT_UP | 缺失 | 未取得权威材料 |
| LIMIT_DOWN | 缺失 | 未取得权威材料 |
| CORPORATE_ACTION | 缺失 | 未取得权威材料 |

## 最终状态

| source | datasets | assessment | REAL 稳定运行数 |
|---|---|---|---:|
| XTQUANT | 六类 | BLOCKED_EXTERNAL_VALIDATION | 0 |
| BAOSTOCK | 六类 | BLOCKED_EXTERNAL_VALIDATION | 0 |
| AKSHARE | 六类 | BLOCKED_EXTERNAL_VALIDATION | 0 |
| TUSHARE | 六类 | BLOCKED_EXTERNAL_VALIDATION | 0 |

PRIMARY/FALLBACK 角色未变，全部 `auto_switch=False`。没有生产 DifferenceReport、
SourceOverride 或 DatasetVersion。当前四来源仍不得凭离线框架升级为 PRODUCTION_READY。
24 个 source×dataset 单元仍保持真实验证阻断，除非未来 S1-07C 产生合格 REAL evidence。

## 验证与副作用

### 定向测试

- S1-07B-FINAL-R1 独立攻击测试（受控 trust release、任意重新自签、时间关系、统一 CLI、
  失败取证及线程/进程并发发布）：`tests/data/validation/test_s1_07b_final_r1.py` —
  `14 passed`。
- S1-07B 新增攻击测试（v4 sample、v3 bundle、时间语义、按 case 放行）：
  `tests/data/validation/test_s1_07b_final.py` — `46 passed`。
- 全部 validation 与 Stage 1 文档回归：`261 passed`。
- assessment persistence、DataCenter、migration 和架构扩展回归：`109 passed`。

### 完整质量门

前两次启动因宿主命令等待上限误设为 1 秒，在 Ruff 前被终止且没有测试失败；修正等待上限后
原样完整运行退出码为 0。最终后端为 `1223 passed, 2 skipped, 3 deselected`；Ruff、format、mypy、
架构边界和 Markdown 通过；前端为 `17 passed`，生产构建通过，Playwright 为 `1 passed`。

### Alembic

Alembic 唯一 head 仍为 `20260729_0012`；S1-07B-FINAL 没有新增 migration。

### 外部访问与副作用

- 外部读取：仅离线读取已封存 P2B/P2F evidence 目录用于复验 manifest/inventory/SHA 和
  构建 reference bundle。不访问网络、QMT、供应商 SDK、旧项目或备份根。
- 外部写入：仅在
  `E:\QuantKingV3Data\validation\s1-07b\reference-bundles\st-603377-authority-v3`
  创建最终 reference bundle。先前的 v1/v2 bundle 原样保留作失败取证/不可变历史；不修改
  P2B、P2F 或任何既有证据。
- Git 只记录规范化事实、locator、材料 SHA、bundle SHA、版本和合同。不提交 PDF、网页或
  证据目录。
- 副作用仅限仓库源码、测试、文档、离线临时测试产物，以及完整质量门对仓库内
  `frontend/node_modules` 和 `frontend/dist` 的常规刷新；没有真实数据或凭据。

## 结论

- S1-07B 已于 2026-08-04 在提交 `d60559601efd61de15b41ffa86b4f2f730a69d5e`
  完成主验收。
- S1-07C/D、S1-08 未开始、未获授权。
- Stage 1 尚未完成。
- REAL 稳定运行数：0。
