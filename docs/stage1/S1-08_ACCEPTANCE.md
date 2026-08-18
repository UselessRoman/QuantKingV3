# S1-08 / Stage 1 最终独立验收记录

## 验收身份与范围

- 验收任务：`S1-08R3D2`、修复复验 `S1-08R3D2-R2`。
- 验收日期：2026-08-18。
- 验收角色：独立验收 Agent；不是 S1-08R3 或 S1-08R3F2 的开发 Agent。
- Stage 1 基线：`9f5b9ea`（S1-08R2 发布引擎收口）。
- S1-08R3 开发候选：`eac4eac10816747dff13002c9fab7655c1bddb27`。
- R3D2 两项阻断修复：`d95db32`（`fix(data): enforce exact S1-08R3 guard contracts`）。
- 验收方式：只读审查 Git 差异和仓内冻结证据、纯离线负向探针、离线测试与完整质量门禁；
  未访问或修改真实运行数据根，也未重放任何历史任务。

## 权威合同与 ADR-028

验收遵循 `docs/DECISIONS.md`、`docs/IMPLEMENTATION_PLAN.md`、
`docs/TECHNICAL_STANDARDS.md`、数据/总体架构、Stage 1 合同与验收矩阵。ADR-028 的
个人本地信任模型是现行决定：内容 SHA 用于完整性和漂移检测，不是身份认证；不要求 PKI、
数字签名、多层 grant、全数据根文件 manifest 或新 R3C 实测。内部证据可记录标准本地数据根；
Web、DataCenter API 和最终用户响应仍不得返回本机绝对路径。该信任模型不降低双实际来源、
K4 阈值、共同缺失拒绝、发布原子性、不可覆盖或 `XTQUANT_LEGACY` 隔离要求。

## 两轮独立验收

第一次 R3D2 对 `eac4eac` 的证据、82 行逐键证明和完整门禁均复验通过，但发现两个生产守卫
合同缺口，因此判定 FAIL：E1 `allowed_write_paths` 被折叠为字典比较，不能拒绝重复/乱序；
C1 `network_scope` 只检查字段存在，不检查冻结值。该轮聚焦测试为 **168 passed**，完整门禁
后端为 **1437 passed / 6 skipped / 3 deselected**，前端 17 passed、Playwright 1 passed。

R3D2-R2 独立审查 `eac4eac..d95db32` 后确认：

- E1 现在比较完整有序 `(relative_path, maximum_entries)` 元组序列；合法模板通过，重复和
  乱序均以 `ENVELOPE_WRITE_PATHS_INVALID` 拒绝。
- `run_formal_entry` 在 attempt ledger、runtime 构造和 provider 调用之前执行该边界检查；
  负向探针确认拒绝后 ledger 不存在。
- C1 使用冻结常量 `CALENDAR_BOOTSTRAP_NETWORK_SCOPE`；任意漂移均以
  `CALENDAR_PLAN_INVALID` 拒绝。
- 修复提交没有修改 `docs/stage1/evidence/`、发布/仲裁/verifier 数据逻辑或任何冻结正式事实。

R2 定向命令为：

```powershell
. .\scripts\activate-toolchain.ps1 -UvCacheDirectory .uv-cache
scripts\uv.ps1 run --offline pytest `
  tests/data/eod/test_calendar_bootstrap.py `
  tests/integration/data/test_s1_08r3_formal_entry.py `
  -q
```

实际结果：**58 passed in 11.87s**。

## C1、E1 与双幂等正式事实

- 唯一 PUBLISHED 日历版本：
  `data_version:083d7fb7-e268-45a6-8c19-19e144f2d498`；150 行、52 个开市日，SH/SZ，
  覆盖 `2026-05-27..2026-08-09`。前三次 C1 attempt 均在 provider/业务写入前失败，
  第四次 `S1-08R3-C1-ed043f32f794` 成功且 `query_trade_dates` 恰好调用一次。
- E1 Job：`job:cd282234-078b-47a9-b17c-12d82e3b7f50`，状态 `SUCCEEDED`；授权
  `S1-08R3-E1-27dec9db8517`、marker `S1-08R3-E1-FORMAL-RELEASE-V1`、幂等键
  `s1-08r3-e1-formal-release-20260817-v1` 均一次消费。
- 唯一正式 PUBLISHED 未复权日线版本：
  `data_version:f913f915-0a73-476a-9513-ebdca087dd90`；请求 2 证券、
  `2026-06-11..2026-08-09`，实际 coverage `2026-06-11..2026-08-07`，82 行、2 证券。
- R3C command 幂等返回既有 E1 Job（`created=false`）；production `_reuse_published`
  路径的审计 Job 为 `job:b5027b23-6058-4204-8f6e-e81aadfbd8a2`。A/B/C provider 调用均为
  0，没有第二个 PUBLISHED 版本；旧第二次 R3C 属 ADR-028 接受的流程偏差，本验收未重跑。

## R3V2 逐键证明

第一次 R3D2 已独立复算仓内 JSONL，而非只统计 `passed=true`：82 个唯一
`(code, trade_date)` 键，证券恰为 `000001.SZ`、`600000.SH`，日期
`2026-06-11..2026-08-07`；A/B 实际来源行各 82，C=0。每行 A/B canonical row SHA、
published row SHA、DifferenceReport 与 Resolution 引用均可重算；selected source 全部为
BAOSTOCK，PUBLISHED 七字段逐项等于 selected 实际行。OHLC/`pre_close` 最大绝对差 0；
volume 最大差 50；amount 最大差 49.72；相对阈值为 0。不存在单来源、共同缺失、冲突、
不合格行、静默 C 替换或 `XTQUANT_LEGACY` 血缘。

正式绑定对象：

- DifferenceReport：`data_diff:193d13c4-198e-4c11-85f4-0b86f61fd046`
- Resolution：`data_resolution:d42d6e2d-cfe7-42bb-8403-bca5b77ef46c`
- Quality：`data_quality:4b3ec4b4-f852-4d4f-89a7-bbf45327da31`

R3V2 关键 SHA-256：

| 证据 | SHA-256 |
|---|---|
| plan | `6f7f906e066a3d35fccadb0e2a3bbc52e3575e61612f6a18e92fbbee0e3c686c` |
| result | `dcdcdd24351ee0c7c89b95cea240d709ea498bb1e48cf24280a178d3503934dd` |
| row proof JSONL | `6c96fe46ca1ffffb6259198b686568a31ae08a28437d54a7f8e5d4e4b6929591` |
| artifact manifest | `2ca3d1b7af065450cef4e30be2219dfa701285bbb6f62f00f17ed19fdcde61af` |
| attempt header | `125be79f656ec2bf444af75ece67707bf99a27066ab4670278fa7a6acc312f7e` |
| completed | `8a1dbea802bd8a294574cdc390b4c37125f9f58719a5bfb146831ca58144f4cd` |

内部 plan SHA 为 `cff0c1a8f791cf80addb2f91c72b85abd2d871f3b34f7f6403cc13a18f7473c4`，
verifier 脚本 SHA 为 `37bfe4bf82b50a5cd13fdc603f9128075b5c45295728190de2ed44dbf5bd6b78`。
R3V2 状态 `VERIFIED`；网络/写入/checkpoint/新 Job/新版本/新产物均为 0。运行数据根前后
均为 420 文件、4,480,964 字节、聚合 SHA
`33b331e8864728c5cb5d4e81ff452af46a386338e8907df311eed3d12436cbc5`，无 WAL/SHM/journal。
E1R 冻结根指纹为
`299ccba9cbb1c7a7ca7ccee92763a265ddb8515d8baee261495ef312b0ecf0da`。

## 旧 XTQuant 最终处置

ADR-027 冻结结论保持有效：发现 5,208 文件、527,708,270 字节；imported=0、included=0、
published=0；第三次 pilot、全量诊断和 full 均未执行。旧根未修改，`XTQUANT_LEGACY`
未进入 E1 lineage。拒绝迁移是 Stage 1 接受的最终处置分支，不表示旧 XTQuant 已迁移。

## 完整质量门禁

R3D2-R2 在文档更新前运行：

```powershell
scripts\quality.ps1 -UvCacheDirectory .uv-cache
```

退出码 0：uv lock、frozen sync、Ruff lint、Ruff format、mypy、架构边界、Markdown links、
前端 npm clean install/typecheck/unit/build、Playwright 和 `git diff --check` 全部通过；离线
pytest **1440 passed / 6 skipped / 3 deselected**，前端 **17 passed**，Playwright
**1 passed**。文档与状态更新完成后再次运行同一完整门禁，最终结果为退出码 0：离线 pytest
**1442 passed / 6 skipped / 3 deselected**，前端 **17 passed**，Playwright **1 passed**；
其余门禁继续全绿。

## 外部访问、边界与非目标

本两轮独立验收合计：网络访问 0；`E:\QuantKingV3Data` 访问 0；旧项目
`E:\QuantKing` 访问 0；`F:\QuantKingV3Backups` 访问 0；真实任务重放 0；新 Job、
DatasetVersion、artifact 或 PUBLISHED 产物 0。所有历史 authorization/marker 已消费且
不可重放。本验收不授权新的网络、数据根访问或后续阶段行为。

Stage 1 只证明明确两证券、明确日期范围的未复权日线，经双实际来源验证后可版本化发布和
显式查询。它不证明全 A 股正式覆盖、复权行情、point-in-time 股票池、无幸存者偏差数据、
研究/因子/模型/组合/回测能力，也不表示 Stage 2 已启动。

## 最终判定

**S1-08R3D2-R2：PASS。S1-08 完成，Stage 1 完成。**

S1-07K4 与 S1-08R2 的既有独立验收继续有效；第一次 R3D2 的两个阻断已由 `d95db32`
关闭并经独立负向探针、定向测试和两次完整门禁复验。Stage 2 的前置条件现已满足，但
Stage 2 实施仍未获得用户单独授权、尚未开始；下一步只能在用户另行授权后启动 S2-01。
