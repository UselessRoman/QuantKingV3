# S1-07A-R1 REAL 证据可信性修复交接

状态：S1-07A-R1 修复已提交、等待主验收；24 个单元仍为
BLOCKED_EXTERNAL_VALIDATION；S1-07B/C/D、S1-08 未开始、未获授权。

## 范围与基线

- Stage：Stage 1 / S1-07A-R1。
- R1 基线：`64d3bc6a3e3b18a14945411b87d42b6182c64835`。
- 只修复 provider-neutral REAL 可信链、稳定运行合同、逐次授权边界、持久化复验和离线测试门。
- 没有迁移数据、发布 `DatasetVersion`、创建 `SourceOverride`、启用 auto-switch 或开始
  S1-08/Stage 2。

## 首次红测

命令：

```powershell
uv run --offline --frozen pytest tests/data/validation/test_s1_07a_r1_attacks.py -q -p no:cacheprovider
```

R1 首次结果为 `22 failed, 1 passed`。根因为 writer/manifest 信任调用方 REAL 与 gate
布尔值、QualityRun 只作浅 JSON 检查、稳定性只数 run_id、缺少 request/fingerprint/
invocation/authorization 合同，以及 repository 主要依赖 Python seal。

## 固定样本 v1

- 版本：`s1-07.fixed-sample.v1`
- SHA-256：`bc40271a632e0f8b4bae493806bf3d316897857a1102ed01b70f2bb46c632bee`
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

- writer 信封只保存 run/request/version/时间等不可变事实，不接受或写入调用方
  `evidence_kind`/gate/content SHA/quality verdict。
- verifier 从规范 request manifest 与完整固定 case 集、受管理 Raw verifier、严格 Canonical
  schema/主键/排序/覆盖与统一 content SHA、完整 PASSED QualityRun 及其版本血缘、严格
  DifferenceReport 行证据/汇总/阈值/双方内容血缘，确定性推导五项 gate。
- REAL 身份仅来自受信 runner invocation attestation；manifest 自报 `REAL` 无效。
- `run_fingerprint` 完整包含 source、dataset、固定 request/case、sample SHA 和
  adapter/package/code/schema/rule/threshold/sample/config 版本。
- 三次稳定运行要求 fingerprint 相同，且 run_id、invocation_id、session_id、开始/结束时间
  均独立；任何运行失败、gate 失败、凭证漂移或样本不合格均保持阻断。
- writer 使用 `.incomplete` 构建、规范自签名 manifest、逐文件 SHA/大小/行数、`COMPLETE`
  和原子 rename；相同 ID/相同字节为 no-op，不同内容冲突。
- verifier 拒绝 manifest、COMPLETE、文件集合、文件 SHA、路径和 lineage 篡改，以及
  symlink/junction/reparse point 和路径逃逸。
- SQLite assessment 保存入口只接受深层验证签发的 `VerifiedAssessmentEvidence`，并在保存前
  重新推导生产状态、核对统一 fingerprint、证据路径/SHA和样本资格；Python seal 不是唯一边界。
- `plan`、`--help`、import、`status`、`verify` 不导入供应商 SDK。`execute` 的固定
  acknowledgement 只作交互确认；必须先验证逐次授权的 authorization_id/source/dataset/
  output_root/sample SHA/有效期，随后才允许 import runner。S1-07A 没有 runner，匹配授权后
  稳定返回 `RUNNER_UNAVAILABLE`、退出码 2，不泄漏 traceback。

## 离线 CLI

```powershell
python scripts/run_source_validation.py plan --source XTQUANT --dataset exchange_calendar --output-root <authorized-absolute-root>
python scripts/run_source_validation.py execute --source XTQUANT --dataset exchange_calendar --output-root <authorized-absolute-root> --ack I_ACKNOWLEDGE_AUTHORIZED_REAL_SOURCE_VALIDATION --authorization-file <one-time-authorization.json>
python scripts/run_source_validation.py verify --validation-root <authorized-absolute-root> --directory <evidence-directory>
python scripts/run_source_validation.py status --validation-root <authorized-absolute-root>
```

`plan` 只展示固定 case、范围、字段、SDK、未来 execute 的网络/QMT访问、输出目录、副作用和完整
无密命令。失败时序为 acknowledgement → 逐次授权 scope/有效期 → runner import → session/
evidence write；任一授权不匹配均在 import、会话创建和证据写入前失败。S1-07A 不包含任何
provider runner，故即使 acknowledgement 和授权均有效也只能得到稳定阻断。

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
- R1 攻击测试：REAL 伪造、request/Quality/DifferenceReport 缺口、九类 fingerprint 漂移、
  三类 invocation 独立性伪造、authorization/runner 失败时序均通过；伪造
  `PRODUCTION_READY` wrapper 被 repository 保存前复验拒绝。
- 定向 validation/repository/Stage 1 文档回归：`99 passed`。
- `scripts/quality.ps1`：通过。
- 离线 pytest：`1020 passed, 2 skipped, 3 deselected`。
- 前端单测：`17 passed`；生产构建通过；Playwright：`1 passed`。
- Ruff、mypy、锁文件、架构边界、Markdown 链接和 `git diff --check` 全部通过。
