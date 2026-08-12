# S1-08 任务卡：正式历史日线版本与 Stage 1 最终验收

## 状态

状态：首次 AKShare 三源正式运行已失败并保留证据；后续执行暂停，等待
[S1-07K3](S1-07K3_TASK.md) 完成 BaoStock 接入、四源仲裁重基线和旧 XTQuant 日线
`DATA_ONLY` 迁移。所属 Stage：Stage 1。Stage 2 未开始。

本卡不再授权或指导旧的 AKShare 三源方案重跑。K3 未提交并通过独立主验收前，不得创建新的
S1-08 正式 Job。

## 首次正式运行的不可变事实（2026-08-12）

首次固定范围是 `000001.SZ`、`600000.SH`，目标窗口
`2026-06-11..2026-08-09`，15 日 lookback 为 `2026-05-27..2026-08-09`。唯一 Job
`job:4c7c9ecf-ca97-425a-88f1-cd24d7e7c7b5` 终态 `FAILED/RESOLUTION_BLOCKED`：

- 东财、腾讯、新浪实际 endpoint invocation 各 4 次，共 12 次，应用层零重试；锁包源码
  HTTP 上界估算 24，不冒充实际 HTTP 计数。
- 东财、腾讯各形成 82 行/2 证券 Raw；新浪形成 41 行与 40 行 Raw；四个单源质量运行均
  `PASSED`。
- 5 份 DifferenceReport 形成 82 个决定：default 1、exception 1、conflict 80。
- OHLC/`pre_close` 没有冲突。东财/腾讯 amount 最大绝对差 50 元；涉及新浪的 volume 最大
  差 50 股，amount 最大差 50 元。
- 候选 `data_version:a4403bce-1dd3-4686-a3c6-f50e8a0b7604` 已安全收束为 `FAILED`；没有
  `PUBLISHED` DatasetVersion 或 Warehouse 发布目录。
- 首次授权已消耗，失败 Raw、质量、Difference、Resolution 和 Job 日志必须保留，不能修改
  规则后把旧 Job 改写为成功。

该次失败证明三源价格一致，也证明全字段零容差不适合直接推广。它不证明 BaoStock、旧
XTQuant 数据、全市场覆盖或新的比较规则已经可用。

## K3 前置产物

S1-08R2 开始前必须有以下已提交、已独立验收产物：

- 精确锁定并实测的 BaoStock 适配器、单位/复权/停牌语义和稳定错误合同；
- `BAOSTOCK + AKSHARE/EASTMONEY|TENCENT|SINA` 固定四来源 EOD 路由；
- 由代表性真实证据支持的新比较规则版本，历史零容差版本保持可复验；
- 旧 `E:\QuantKing\data\kline\1d` 全部候选文件的只读清单、受管导入、转换、复核、隔离
  和缺口报告；
- 旧根迁移前后指纹一致，正式数据根中每个导入对象可由源相对路径和 SHA-256 追溯；
- DataCenter 和 release verifier 已支持 BaoStock、三个 AKShare upstream 与
  `XTQUANT_LEGACY` 血缘；
- K3 完整离线质量门、真实探针结果和独立验收提交。

## R2 固定范围的形成方式

R2 的范围不能沿用用户估计或旧任务的两只证券。联网/正式写入前由 K3 已验收 manifest 生成
并由用户授权，至少冻结：

| 项 | 必须冻结的内容 |
|---|---|
| dataset/schema | `daily_bar_unadjusted` / `daily_bar_unadjusted.v1` |
| legacy import | 明确 import manifest ID/SHA 与发现、导入、隔离计数 |
| securities | 实际支持并完成复核的规范代码集合 SHA；不得只写“全 A 股” |
| historical coverage | 每证券实际最早/最晚日期和全局汇总，不使用估计截止日 |
| incremental window | 历史 cutoff 后到授权日的明确目标窗口和 lookback |
| sources | BaoStock、东财；异常时腾讯、仍异常时新浪；历史含 `XTQUANT_LEGACY` |
| rule versions | 四源路由、单位、比较、质量、代码版本 |
| network budget | 按来源和 chunk 的 planned/maximum invocation；应用层重试 0 |
| data root | `E:\QuantKingV3Data` |
| publish intent | `PUBLISH_AFTER_QUALITY` |

若全量复核仍有大量未解决冲突，R2 可以发布明确的已复核子集，但必须满足以下两点：范围由
manifest 精确列出，且所有排除项在迁移报告中逐类守恒。不得把子集称为全市场全历史。

## 实现与执行步骤

### 1. 离线预检

- `scripts/quality.ps1` 完整离线通过，所有真实 marker 排除。
- dry-run 只读输出 K3 产物、输入指纹、证券集合 SHA、历史/增量 coverage、A/B chunks、C/D
  最大异常 chunks、调用预算和目标目录；零网络、零新数据库/Raw。
- 正式数据根中的首次失败、Stage 0 acceptance/validation 和 K3 导入证据均保留；出现未知
  数据、悬挂迁移或数据库 revision 不一致即停止。
- release verifier 先对 K3 import manifest 和所有拟复用 Raw 做只读复验。

### 2. 创建唯一 R2 Job

- idempotency key 必须绑定全部冻结输入，不能复用首次失败 key。
- 由正式 Web/CLI 共用的 `EodCommandService` enqueue，并由正式 Worker 执行；不允许测试 fake、
  直接适配器调用或人工写数据库绕过 Job。
- 历史已复核对象按 manifest 合并；增量范围正常调用 BaoStock A 与东财 B，异常范围才逐级
  调腾讯 C、新浪 D。
- 所有调用在网络前完成预算 preflight；预算不足、单源失败或质量失败按合同停止/仲裁，应用层
  不自动重试，不在同一授权下扩大范围。
- 成功只产生一个最终候选和一个 `PUBLISHED DatasetVersion`；失败保留现场并收束候选，不能
  发布半成品。

### 3. 独立只读复验

`scripts/verify_s1_08_release.py --version-id <explicit-id>` 只接受当前 data root 的规范 ID，
拒绝 `latest`、路径或 SQL，并至少复验：

- 数据库 revision、Job/Version 终态、manifest/`COMPLETE`、文件集合与内容 SHA；
- import manifest、旧源相对路径/SHA、发现/导入/隔离计数守恒和旧根只读证明；
- 证券集合 SHA、每证券 coverage、主键唯一、确定排序、OHLC、`pre_close`、非负量额和最终
  QualityRun `PASSED`；
- 每个已发布行的两源共识、实际差值、规则版本和选择来源；未复核/冲突行不在版本中；
- 全部实际 BaoStock/AKShare Raw、未调用 C/D 不存在伪 batch、预算 planned/actual 守恒；
- DifferenceReport、ResolutionRun/Decision、默认/例外/冲突、失败候选和发布血缘；
- DataCenter 以显式 version 有界展示覆盖、来源、质量、差异、迁移缺口与预览，且不泄露绝对
  路径或供应商载荷；
- verifier 以只读方式运行，正式数据根前后文件集合、mtime、大小和 SHA 不变。

### 4. 幂等、恢复和 Web

- 同一 idempotency key 重复 enqueue 返回原 Job，零网络、零新版本。
- 新 key 但相同冻结输入只在深验完整 `PUBLISHED` manifest 后复用同一 version，零网络。
- 中断 Job 标记 `INTERRUPTED`；用户另行授权的新 Job 才能重试，不续跑旧 Job。
- DataCenter 同时显示历史导入覆盖、服务源覆盖、未复核/冲突计数和实际来源健康，不把
  “有 Raw”展示为“已发布”。

## Stage 1 最终验收

必须全部满足：

- 至少一个明确证券集合和日期范围的未复权日线版本处于 `PUBLISHED`。
- 旧项目全部候选文件均被发现、导入或以明确读取/语义原因隔离，计数和字节守恒；旧项目未
  被修改，未启动 XTQuant/QMT。
- 发布版本中每一行至少有两个独立来源在冻结规则内一致；容差没有掩盖缺行、复权/单位不明
  或质量失败。
- BaoStock+东财正常路径以及腾讯/新浪逐级异常路径有真实或明确范围证据，调用和失败受预算
  约束；新浪不承担常态全量抓取。
- Raw、导入、质量、Difference、Resolution、Warehouse 和 Job 血缘可由独立 verifier 重建。
- 两种幂等路径零网络且不产生新版本；失败批次不污染已发布版本。
- 北交所、退市、复权、公司行动、历史 ST/停牌和动态股票池中仍未覆盖的内容逐项列出，禁止
  宣称 Stage 2 研究就绪。
- 真实运行后再次完成锁文件、Ruff、format、mypy、全部离线 pytest、前端 typecheck/unit/
  build、Playwright、Markdown 和 `git diff --check`。
- 新建 `docs/stage1/S1-08_ACCEPTANCE.md`，记录实际版本 ID、coverage、来源/迁移计数、质量门和
  保留缺口。只有主 agent 独立复验通过，才能声明 Stage 1 完成。

## 授权边界

当前决定只授权修改任务卡和文档，不自动授权：

- 安装 BaoStock 或连接 BaoStock/AKShare；
- 读取 `E:\QuantKing` 或写 `E:\QuantKingV3Data`；
- 执行 K3/S1-08 真实探针、全量复核或失败重试；
- 访问 F 盘、启动 QMT/旧项目/券商客户端；
- 开始 Stage 2。

实际执行必须在 dry-run 后由用户按固定源根、目标根、证券/日期、调用预算、写入字节和零重试
条件重新授权。
