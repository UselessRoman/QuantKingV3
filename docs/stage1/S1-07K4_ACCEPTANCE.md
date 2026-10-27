# S1-07K4 独立验收记录（K4E）

本记录是不可变验收事实记录。验收通过后只追加新记录，不原地修改本文件。

## 1. 验收日期与角色

- 验收日期：2026-08-17。
- 验收角色：K4E 独立验收 agent。该验收上下文未参与本轮 K4A/K4B 实现编写、K4C 证据采集
  或 K4D 执行；代码、证据与边界复核由四个独立 review agent 分别对 K4A/K4B/K4C/K4D
  执行，验收结论以仓库代码、冻结 evidence 字节、离线 verifier 与实际门禁输出为依据，
  不复述实施记录。
- 验收范围：S1-07K4 全部工作（K4A 三角色生产路线、K4B 仲裁/质量语义、K4C 代表性真实
  证据与 K4 0/50 阈值、K4D 候选评估与拒绝收口）及单一 K4 提交边界的形成。

## 2. 基线与提交边界

- 基线提交：`b8f4859`（`main`，docs(stage1): define ordered three-source closeout）。
- 验收时 `git status --short` 显示的全部未提交修改、删除与新增文件均逐项分类为
  S1-07K4 范围（三角色运行时重构、K3 退役文件删除、K4 模块/测试/evidence、状态文档），
  无用户无关修改混入。
- 本验收记录与 S1-07K4 全部变更位于同一个 K4 提交中。

## 3. K4A 分项结论：三角色生产路线——通过

1. 正式来源仅为 A=BaoStock、B=AKShare/腾讯、C=AKShare/新浪
   （`src/quantking/data/adapters/sources/registry.py` 的 `PRODUCTION_UPSTREAMS`）。
2. 正常路径只调用 A/B；仅当 A/B 失败、缺行、质量异常或分歧时，才对受影响范围调用 C
   （`src/quantking/data/resolution/service.py` 的 affected-span 路由与
   `AB_SOURCE_UNAVAILABLE`/`AB_AFFECTED_RANGE` 触发原因）。
3. 任意候选发布行至少有两个实际持有该行的独立 upstream 在冻结阈值内一致
   （`_agreement` 只对实际持有行的来源做两两一致性判定）。
4. 共同缺行不构成数值共识（缺行侧不参与一致性组合；整范围零行需 ≥2 个 EMPTY 来源才可
   形成 `ZERO_ROW_LEGAL_EMPTY_CONSENSUS`，否则置 blocker）。
5. 东财不再进入 runtime、registry、endpoint 预算、新 Job 指纹或正式调用路径；残留引用
   全部为历史枚举、K2/K3 历史规则版本、Raw/migration/evidence 读取兼容与"东财被拒"
   测试，未发现任何新的东财正式调用路径。
6. AKShare 腾讯与新浪分别记录 provider/upstream、endpoint、Raw、版本、质量和错误
   （独立 `SourceLineage`、独立 Raw chunk 与 manifest 字段）。
7. A/B 一致时 C 的 planned/actual 均为 0（SINA 必须显式 preflight 才有非零计划，
   `before_endpoint` 对未计划调用直接拒绝；`test_sina_stays_zero_planned_and_actual_when_a_and_b_agree`
   等测试覆盖）。
8. endpoint 预算、子进程硬超时、稳定错误码、失败即停均有测试
   （`test_endpoint_invocation_budget.py`、`test_akshare_adapter.py` 硬超时/稳定错误码用例、
   `test_eod_worker_k4.py` 预算阻断/失败即停用例；`application_retries` 恒为 0）。

## 4. K4B 分项结论：仲裁与质量语义——通过

1. `K4ExpectedSessionResolver` 只消费冻结且已发布的 exchange calendar 绑定与
   security exceptions（`ReferenceDataBinding` 强制 data_version 命名空间与内容 SHA；
   绑定缺失/无效/不完整均 fail-closed）。
2. 上市前合法整范围空与停牌走不同事实路径，不重复计数（合法空仅限整范围零行 +
   日历/例外证明；停牌日缺行由例外扣除，停牌日出现行计为 unexpected）。
3. 预期交易日缺行不能被解释为合法空（expected trading days / freshness / calendar
   membership 规则对缺行判 FAILED；伪造 empty_result 不能绕过日历）。
4. error、partial、unexpected empty 不得降级为空结果（`CALL_FAILED`/`QUALITY_FAILED`
   不在可用集合，进入 affected 或 conflict，绝不转为 EMPTY）。
5. 停牌占位不生成虚假 bar、不污染 `pre_close` 推导（生产路径无占位合成逻辑；
   K4C-R3 诊断中的停牌占位仅是上游 Raw 事实记录）。
6. `pre_close` 统一由完整未复权序列前一行 close 派生（`_evaluate_pre_close_consistency`
   按证券逐行强制；vendor 参考昨收只保留 Raw）。
7. OHLC/`pre_close` 零容差、volume/amount 绝对容差 50、相对容差 0
   （`comparison-thresholds.daily-bar-unadjusted.k4.v1`，`K4_THRESHOLD_ACCEPTANCE`
   冻结 bounds 0/0 与 50/0）。
8. 缺少两源一致时必须隔离并阻止发布（conflict/blocker 存在时
   `candidate_version_id=None`，`ResolutionRun` 结构上禁止可发布结果携带冲突）。
9. 不存在静默换源、默认阈值放松、SourceOverride 回流或生产降级路径（生产 worker 显式
   注入 K4 阈值；默认回退阈值为更严格的全零 K1；`SourceOverride` 表仅存于历史 schema，
   K4 仲裁路径完全不读取）。
10. Raw/质量失败保留稳定、脱敏且可复验的原因链（稳定 StrEnum 错误码/原因码 + SHA +
    受管相对路径行级证据；第三方异常文本不外泄）。

## 5. K4C 分项结论：代表性真实证据——通过

历史事实（不得改写）：

- K4C-R1（2026-08-15 attempt a）独立验收不通过，`AttributeError` 失败即停，
  `sufficient=false`；证据字节冻结。
- K4C-R2（2026-08-16）36/36 调用成功，`sufficient=false`（三个 objective 未在本窗口
  出现），如实记录。
- K4C-R3（ADR-022 + Phase B 9/9）`sufficient=false`（suspension_session 未观测），
  如实记录。

当前有效证据：`docs/stage1/evidence/k4c_r4_composite_evidence_2026-08-16.json`
（schema `s1-07k4c-composite-evidence.v2`，K4C-R4 离线验收修复层，非新真实网络 attempt）：

- SHA-256：
  `87b04863756a6e92614f62fe26cfb7d8b1b591c101bcc28d902a7fc3c1bdd300`（本次验收独立
  重算一致）。
- 248 行 held rows 可离线重算（`scripts/history/stage1/build_composite_evidence.py --schema v2`
  纯离线重算，verifier 通过后才落盘；`test_real_r4_composite_verifies_offline` 通过）。
- objective 8/8 全部 observed（normal_session、ex_rights_session、suspension_session、
  low_liquidity_session、volume_amount_boundary、missing_or_empty_upstream
  （上市前合法整范围空 688825.SH）、shenzhen_code_segments、shanghai_code_segments）。
- `representative_evidence_sufficient=true`；`threshold_reacceptance_eligible=true`；
  `threshold_coverage.covered=true` 且规则版本为
  `comparison-thresholds.daily-bar-unadjusted.k4.v1`。
- 停牌来源闭链：上交所公告参考 `k4c_r4_references/suspension_688237.SH_2026-07-16_sse.json`
  （SHA `cca0e7d1…cf07`）+ BaoStock raw_fact `tradestatus=0`。
- 关联组件 SHA：R2 evidence
  `217390b53e03345ac84a97739a88cb28b05299f9d2d2bc9ec036d7af89381835`、R3 evidence
  `1632ee8b2a210bcbe22742386072d9c16a10c789cc4bba9bd8dcf5f4ed0836ea`、composite v1
  `910010da4c309cb93faacc2a7705b4a15cfc2279cfbe762112a86a4fa9b8f12d`（历史产物保留）。
- K4 阈值已按用户决定接受重新冻结（ADR-024，accepted_at 2026-08-16，
  `K4_THRESHOLD_ACCEPTANCE` 冻结记录与 API 值一致，由
  `test_k4_threshold_acceptance_record_is_frozen` 守卫）。
- 全部冻结 evidence、raw envelope、manifest 与独立 reference SHA 保护测试通过
  （`test_k4c_historical_evidence.py`、`test_k4c_r1_evidence.py`、
  `test_k4c_r3_evidence.py`、`test_k4c_r4_evidence.py`）。
- 本次验收未执行任何新的真实网络调用，未重放任何历史 authorization_id。

## 6. K4D 分项结论：候选评估完成、迁移拒绝——通过

正确结论：**K4D 迁移未成功；K4D 候选评估及拒绝决策已完成。**

- Inventory（ADR-025，2026-08-16 一次授权已消费）：5,208 文件、527,708,270 字节、
  16,786,177 readable rows、零读取错误、与 Stage 0 基线差异 0/0；evidence
  `docs/stage1/evidence/k4d_inventory_2026-08-16.json` SHA-256
  `140978ea3e3f4d9139c15d641c92564af8f58928bf77017117393ce988a04ce2`（本次验收独立
  重算一致）。
- Pilot attempt 1（2026-08-16，`K4D-PILOT-30fb6c198a0e`）：第 6 次调用
  000858.SZ × 新浪失败，失败即停，staging 零写入；`FAILED` 状态与证据目录字节冻结，
  不得改写。
- Pilot attempt 2（2026-08-17，`K4D-PILOT-d0280d367480`）：18/18 endpoint 成功、
  24 条 journal 全 SUCCESS、Canonical 17,585、首行排除 6、隔离 8,609、守恒
  17,585+6+8,609=26,200、冻结守卫 `PILOT_CANONICAL_COUNT_UNEXPECTED` 触发、
  staging 零写入；`FAILED` 状态与证据目录字节冻结。
- 诊断 `docs/stage1/evidence/k4d_pilot_attempt2_diagnostic_2026-08-17.json` SHA-256
  `b75f27b4096571f04eb790e8cd78f1da1faf174187dc44ab0ca09d20fe1c02e1`（本次验收独立
  重算一致）；隔离构成 8,503 `NEGATIVE_VALUE:open` + 105 `NEGATIVE_VALUE:low` +
  1 `ZERO_VOLUME_PRICE_MOVEMENT`。
- ADR-027（2026-08-17 已接受）：拒绝旧 XTQuant 日线迁移；第 3 次 pilot、5,208 文件
  全量诊断与 K4D full 均不需要、不授权；`XTQUANT_LEGACY` 不进入 ResolutionDecision、
  正式 Raw、staging、Canonical、Warehouse 或发布链。
- 代码复核：`src/` 中 `XTQUANT_LEGACY` 全部出现均为历史枚举、证据/inventory 元数据、
  拒绝路径记录或 `warehouse_published=False` 硬编码守卫的 legacy staging 元数据；
  `ResolutionDecision`（`src/quantking/data/resolution/models.py`）不含
  `XTQUANT_LEGACY`；CLI pilot 仅 dry-run，真实执行器仅存在于默认跳过、需环境变量
  授权的 compatibility 测试中，无第 3 次 pilot 或 full 的活跃入口。
- 原始旧文件未修改；不得把 K4D 描述为迁移成功。

## 7. 关键生产不变量（验收确认有效）

1. 正式来源只有 `BAOSTOCK`、`AKSHARE/TENCENT`、`AKSHARE/SINA`；东财与
   `XTQUANT_LEGACY` 无正式调用路径。
2. 任意发布行有两个实际持有该行的独立来源在
   `comparison-thresholds.daily-bar-unadjusted.k4.v1`（OHLC/`pre_close` 0/0、
   volume/amount 50/0）内一致；共同缺行不是值共识。
3. A/B 一致时 C 零计划零调用；C 只按显式 preflight 的受影响范围调用。
4. error/partial/unexpected empty fail-closed；无两源一致即隔离并阻止发布。
5. Canonical `pre_close` 恒由完整未复权序列前一行 close 派生。
6. 应用层零重试；预算不足在首次调用前拒绝；失败即停保留证据。
7. 已发布产物与冻结 evidence 只追加不覆盖；历史失败事实不得改写。

## 8. 完整门禁实际结果

- K4 定向测试（19 个文件：endpoint budget、source port、expected-session resolver、
  EOD worker/config、resolution artifacts/models/service、quality rules、K4 阈值、
  K4C probe/historical/R1/R3/R4、K4D inventory/pilot、legacy recheck、Stage 1 状态
  文档）：**368 passed**（62.77s，退出码 0）。
- 完整离线门禁 `scripts\quality.ps1 -UvCacheDirectory .uv-cache` 各分项全部通过（两次
  连续执行，结果一致）：
  - `uv lock --check --offline`：223 packages 解析通过。
  - `uv sync --frozen --all-groups --offline`：217 packages 检查通过。
  - Ruff lint：All checks passed。
  - Ruff format check：269 files already formatted。
  - mypy `src/quantking`：Success，130 source files 零问题。
  - 全部离线 pytest（`-m "not real_external and not real_akshare and not
    real_k3_sources and not real_legacy_data and not real_backup"`）：
    **1277 passed、6 skipped、3 deselected**（214.36s，退出码 0；未使用任何 real
    marker、未运行 compatibility real tests）。
  - 架构边界检查、Markdown 链接检查：通过。
  - 前端 npm clean install（offline）、typecheck、unit tests、production build：
    通过。
  - Playwright E2E：1 passed（9.5s，Web server 正常启停）。
  - Git whitespace（`git --no-optional-locks -c core.excludesFile= diff --check`）：
    quality.ps1 内对该 git 进程的直接调用两次被 TRAE 执行沙箱以无关系统后台文件
    活动（TokenBroker/NVIDIA 缓存等）误判拦截，属环境限制而非检查失败；以工作区
    规定的只读 Git 包装 `scripts\git-read.ps1 diff --check` 执行完全相同的 git
    调用，退出码 0、零 whitespace 问题。
- 门禁任一分项失败时本验收不会形成提交；上述全部通过后方执行本次单一 K4 提交。

## 9. 外部访问声明

本次 K4E 验收为纯离线只读验收：

- 真实网络调用：0。
- 旧项目根 `E:\QuantKing` 访问：0。
- 运行数据根 `E:\QuantKingV3Data` 写入：0。
- F 盘数据/备份根访问：0（仅执行系统安装的 pwsh 7 解释器可执行文件本体，未访问任何
  F 盘数据）。
- XTQuant/QMT 启动：0；历史 authorization_id 重放：0；第 3 次 K4D pilot/K4D full：0；
  S1-08R2/R3 开发或正式 Job：0。

## 10. 最终结论

**S1-07K4 独立验收通过。**

下一步是 S1-08R2（发布引擎离线收口），须由单独任务启动；Stage 1 尚未完成，Stage 2
未开始。
