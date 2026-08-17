# S1-07K3 真实执行证据目录

本目录保存 K3B 真实探针的失败与诊断证据。这些文件是不可变事实记录，只追加、不原地修改；
新增证据使用新文件名。

| 文件 | 内容 |
|---|---|
| `k3b_representative_probe_attempt_2026-08-13.json` | V2 代表性 marker 第一次尝试（VPN 开启）：BaoStock/000001.SZ 成功，东财断连即停，余 22 次未调用 |
| `k3b_representative_probe_attempt_2026-08-13_b.json` | 第二次尝试（VPN 关闭）：同结果，附 DNS/TCP 诊断（网络层全通、HTTP 层被断） |
| `k3b_connectivity_probe_2026-08-13.json` | 最小连通性探针（东财/腾讯/新浪各 1 次）：腾讯、新浪 complete，仅东财 FAILED |
| `k3b_eastmoney_ipv6_root_cause_2026-08-13.json` | 根因证据：同一 curl 请求 IPv4 返回 200 完整数据、IPv6 数据断流（curl 56）；ADR-019 依据 |
| `k3b_eastmoney_throttling_2026-08-13.json` | 限流证据：IPv4 钉死生效后，同一 curl 请求从 200 变为 56，python-requests 与 .NET TLS 栈同断 |
| `k3_ftshare_candidate_probe_2026-08-14.json` | FTShare 首轮候选验证：3/3 请求均为 `FtshareHTTPError`，未进入独立源比较 |
| `k3_ftshare_candidate_r2_2026-08-14.json` | FTShare R2 诊断：首个单证券请求返回 HTTP 400，按授权停止于 1/2，零重试 |
| `k3_ftshare_candidate_r3_2026-08-14.json` | FTShare R3 单变量修复：省略 `adjust_kind` 后单证券请求成功返回 52 行，仍未激活生产 B 源 |
| `k3_ftshare_qualification_attempt_2026-08-14.json` | FTShare B 源资格验收：batch 证券标识集合不符，停止于 FTShare 3/3；BaoStock/腾讯未调用，结论不接纳 |

## K4C 代表性实测证据（S1-07K4C）

| 文件 | 内容 |
|---|---|
| `k4c_r2_representative_probe_2026-08-16.json` | K4C-R2 真实实测（36/36 成功、sufficient=false 因三个 objective 未观测），SHA `217390b5…` 冻结 |
| `k4c-r2-attempts/K4C-R2-cab803399515/` | R2 attempt 目录（invocations.jsonl / comparison-rows.json / raw envelopes / artifact-manifest），manifest SHA `04401321…` 冻结 |
| `k4c_r3_probe_dry_run_2026-08-16.json` | R3 零网络 dry-run（三组用户接受的冻结样本、预算 9 次调用、引用 R2 组件 SHA），文件 SHA `174c9546…`、plan_sha256 `38c88f40…` |
| `k4c_r3_references/suspension_688237.SH_2026-07-16.json` | 停牌独立参考（交易所公告），SHA `73e9cbec…` |
| `k4c_r3_references/low_liquidity_688237.SH_2026-07-15.json` | 低流动性独立参考（公开行情统计），SHA `060aac79…` |
| `k4c_r3_references/legal_empty_688825.SH_listing_2026-07-27.json` | 上市前合法空独立参考（上交所上市公告 + 2026 节假日安排），SHA `4ebd0410…` |
| `k4c_r3_references/exchange_calendar_sh_2026-07.json` | 冻结交易日历参考（SH 2026-07-01..27，依据 2025-12-22 节假日公告），SHA `244e6210…` |
| `k4c_r3_diagnostic_baostock_688237_2026-08-16.json` | K4C-R3-R1 有界诊断（1 次 BaoStock 调用）捕获的停牌占位行原始形态，SHA `c31febc8…` |
| `k4c-r3-attempts/K4C-R3-4f9c2e7a1b08/` | R3 Phase B 第 1 次尝试失败证据（1/9，`BAOSTOCK_SCHEMA_DRIFT`，失败即停） |
| `k4c-r3-attempts/K4C-R3-7c51a3d9e2b4/` | R3 Phase B 第 2 次尝试失败证据（7 次调用后 `UNEXPECTED_EMPTY`，失败即停） |
| `k4c-r3-attempts/K4C-R3-2b8e5f14a6c9/` | R3 Phase B 第 3 次尝试（9/9 成功），manifest SHA `2b2f919b…` |
| `k4c_r3_representative_probe_2026-08-16.json` | R3 evidence（9/9 成功、低流动性+上市前空观测、停牌本地未观测如实记录、calendar_proofs），SHA `1632ee8b…` |
| `k4c_composite_evidence_2026-08-16.json` | K4C composite evidence v1（历史产物，保留；8/8 objective、sufficient=true、eligible=true），SHA `910010da…` |
| `k4c_r4_references/suspension_688237.SH_2026-07-16_sse.json` | R4 官方停牌公告参考（688237.SH、公告编号 2026-031、停牌 2026-07-16、来源 exchange_announcement、provenance_hardening_only），SHA `cca0e7d1…` |
| `k4c_r4_composite_evidence_2026-08-16.json` | K4C composite evidence v2/R4（离线验收修复层；重算 248 行、8/8 objective、0/50 覆盖、停牌来源闭链绑定；K4C-R4 独立验收通过，为阈值重新冻结 ADR-024 的依据），SHA `87b04863…` |

## K4D 旧根迁移证据（S1-07K4D）

| 文件 | 内容 |
|---|---|
| `k4d_inventory_dry_run_2026-08-16.json` | K4D inventory 零读取 dry-run 计划（`legacy-inventory-dry-run.k4d.v1`；real marker、`reads_source=false`/零网络/零数据根写入、Stage 0 基线 5,208 文件/527,708,270 字节仅作差异比较、实际执行须另行授权），文件 SHA-256 `46272de4…`、plan_sha256 `f4fee828…` |
| `k4d_inventory_2026-08-16.json` | K4D 真实只读 inventory（2026-08-16 一次授权，marker `S1-07K4D-INVENTORY-REAL-READONLY-V1`，closed scan 197.1s）：5,208 文件 / 527,708,270 字节，与 Stage 0 基线差异 0/0；16,786,177 可读行、零读取错误、零重复键、单一 schema（`code`/`date` string，OHLC/volume/amount double，date=`yyyymmdd`，无 `pre_close` 列）；date 覆盖 19901219..20260603、code 覆盖 000001.SZ..689009.SH；文件 SHA-256 `140978ea…`、source_fingerprint `af42dd43…` |
| `k4d_pilot_dry_run_2026-08-16.json` | K4D pilot 零外部 I/O dry-run 计划（ADR-026；marker `S1-07K4D-PILOT-REAL-MIGRATION-V1`，六固定证券/六文件 SHA/size/rows/coverage 冻结，合计 6 文件/878,893 字节/26,200 Raw 行、预期 Canonical 26,194；窗口 2025-05-01..2026-06-03、18 次调用/HTTP 30/BaoStock 18、20s/60s 超时、零重试失败即停；语义 HYPOTHESIS；`reads_legacy_root=false`/零网络/零 staging/零 F 盘；真实执行须另行授权），文件 SHA-256 `c77b2d8e…`、plan_sha256 `76e928aa…` |
| `k4d-pilot-attempts/K4D-PILOT-30fb6c198a0e/` | **K4D 真实 pilot 第 1 次 attempt（2026-08-16，一次授权已消费，失败即停）**：按 plan SHA `76e928aa…` 执行，第 6 次 endpoint 调用（000858.SZ × 新浪）抛 `SourceAdapterError` 即停（fetcher 顺序推断前 5 次 000001.SZ×A/B/C 与 000858.SZ×A/B 成功、12 次未调用；无逐调用 journal——已识别为证据链缺陷）；`E:\QuantKingV3Data` 零写入（无 `legacy-pilot-imports`/`legacy-pilot-idempotency`）；仅 attempt-header.json（STARTED）+ failure.json（`PILOT_SERVICE_FETCH_FAILED:000858.SZ`）保留，不得改成成功；重试须修复后另行授权 |
| `k4d-pilot-attempts/K4D-PILOT-d0280d367480/` | **K4D 真实 pilot 第 2 次 attempt（2026-08-17，一次授权已消费，失败即停）**：按同一 plan SHA `76e928aa…`、authorization_id `K4D-PILOT-d0280d367480`、idempotency key `k4d-pilot-20260817-v1` 执行（72.6s）；**18/18 A/B/C endpoint 全部成功**（invocations.jsonl 24 条：18 ENDPOINT SUCCESS + 6 SYMBOL SUCCESS，三源各 symbol 窗口行数完全一致 263/263/263/263/258/257=1,567 行）；第 3 步纯转换后守卫 `PILOT_CANONICAL_COUNT_UNEXPECTED` 触发即停——真实旧文件按 K4D 语义隔离 8,609 行（负 OHLC 为主），Canonical 17,585 ≠ 冻结预期 26,194；`E:\QuantKingV3Data` 零写入；attempt-header.json（STARTED）+ failure.json + invocations.jsonl 冻结，不得改成成功；后续执行须先做合同决策再另行授权 |
| `k4d_pilot_attempt2_diagnostic_2026-08-17.json` | 第 2 次 attempt 失败的纯离线诊断（零网络、零数据根写入、只读同六文件）：组合转换 Canonical 17,585/首行排除 6/隔离 8,609（`NEGATIVE_VALUE:open` 8,503、`NEGATIVE_VALUE:low` 105、`ZERO_VOLUME_PRICE_MOVEMENT` 1）/停牌占位 188，守恒 17,585+6+8,609=26,200 成立；逐 symbol 统计（000001.SZ 隔离 2,980、000858.SZ 3,758、600000.SH 1,871、300750.SZ IPO 日负值行为首行排除、688xxx 零隔离）；负 OHLC 集中于 1991–2008 高分红证券早期历史且伴随真实成交（如 000858.SZ 1998-04 open=-30.27、量 341,630），与旧导出早期历史为前复权数据的假设一致，文件 SHA-256 `b75f27b4…` |
| （ADR-027 收口，无新增证据文件） | **K4D 最终处置（2026-08-17 用户决定，ADR-027）**：拒绝旧 XTQuant 日线迁移；第 3 次 pilot、5,208 文件全量诊断与 full 均不执行；`XTQUANT_LEGACY` 不进入发布链。本表所列 inventory evidence、两次 attempt 目录与诊断 JSON 即该决定的全部冻结依据，字节不变、不得改写；两次 attempt 保持 FAILED 历史状态，不伪造成功 pilot/迁移/staging/Canonical 产物；未来重启旧数据研究须作为新任务另行立项授权 |
