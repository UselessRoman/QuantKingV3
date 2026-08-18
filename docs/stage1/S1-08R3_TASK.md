# S1-08R3 任务卡：正式发布与 Stage 1 终验

## 目标与前置

所属 Stage：Stage 1。状态：**已完成**。S1-07K4、S1-08R2 与本任务均已独立验收通过；
最终结论见 [S1-08 / Stage 1 最终独立验收记录](S1-08_ACCEPTANCE.md)。

本任务只执行一次冻结范围的正式发布和终验。前置提交必须包括：K4 三角色/真实实测/旧迁移
验收、R2 发布引擎验收、完整离线质量门，以及用户对明确网络和数据根写入范围的新授权。

## R3A：冻结发布 manifest

正式授权前先输出零网络 dry-run，至少冻结：

- `daily_bar_unadjusted.v1` 和唯一 canonical schema；
- legacy import manifest ID/SHA、发现/导入/隔离计数；
- 规范证券集合及 SHA、每证券历史 coverage；
- 明确增量 target/lookback 和版本化交易日历 SHA；
- BaoStock A、腾讯 B、异常新浪 C 的 chunk 和 endpoint/HTTP 上界；
- 路由、单位、比较、质量、代码版本和 `PUBLISH_AFTER_QUALITY` 意图；
- `E:\QuantKingV3Data` 的精确写入目录集合。

范围不得写成“全 A 股”或用户估计日期。若只发布已复核子集，manifest 必须列出全部排除项并
保持计数守恒。

## R3B：唯一正式 Job

- 使用正式 Web/CLI 共用的 `EodCommandService` enqueue，由正式 Worker 执行。
- 所有调用在网络前完成预算 preflight；应用层零重试，失败即停，不在同一授权下扩大范围。
- 正常调用 A/B；仅异常范围调用 C。没有两个实际来源一致的行隔离，不进入候选。
- 成功只产生一个最终候选和一个 `PUBLISHED DatasetVersion`；失败现场保留并收束候选。

## R3C：独立只读复验与双幂等

- 运行显式 version ID 的只读 verifier，复验完整血缘、内容 SHA、交易日完整性、两源共识、
  规则版本、预算和排除项守恒。
- 相同冻结输入的 command 幂等与已发布版本复用均为零网络、零新版本。
- DataCenter 对明确版本完成有界查询与 Web E2E，不泄露路径或供应商载荷。
- 正式运行后再次完成全部后端、前端、E2E、架构、文档和 Git 门禁。

## R3D：Stage 1 最终验收

- 至少一个明确证券集合和日期范围的未复权日线版本处于 `PUBLISHED`。
- 每个发布行绑定两条实际来源行；共同缺行、单位/复权不明或质量失败没有被容差绕过。
- 旧项目候选全部被发现、受管导入或明确隔离；旧根未修改，迁移计数和字节守恒。
- 创建 `S1-08_ACCEPTANCE.md`，由独立 agent 验收并提交。
- 该独立验收完成后 Stage 1 才可标记完成；Stage 2 仍须用户另行授权，不由本任务自动启动。

## 授权边界

R3A dry-run 不授权真实操作。R3B 的网络、正式数据根写入和随后可能的任何失败重跑必须分别
使用用户批准的固定 marker；一次失败授权不得自动转化为重试、扩容或新的发布任务。

## R3F-LITE 精简修复（2026-08-18，ADR-028）

个人本地信任模型与适度授权控制收口：E1 正式入口 token 升级为内容绑定
`<marker>:<id>:<envelope_sha256>` 并增加写入面模板/日历来源/数据根/attempts root
边界校验；C1 plan 改为强类型全字段校验 + `<marker>:<id>:<plan_sha256>` token；
R3C 未来探针增加简单一次性 attempt ledger（本次不重跑 R3C，旧证据字节不变）；
verifier 新增正式版本逐键证明（A/B 规范化实际行、K4 阈值重算、DR 重算、
selected=PUBLISHED、零 XTQUANT_LEGACY）。唯一新数据根访问为 R3V2 纯只读复验
（marker `S1-08R3-R3V2-READONLY-VERIFY-V1`，已于 2026-08-18 经用户批准执行
VERIFIED：82/82 行逐键全部 passed、A/B 实际行各 82、PUBLISHED==selected 逐字段
相等、OHLC/`pre_close` delta 0、volume≤50、amount<50、零 XTQUANT_LEGACY、前后
聚合根指纹全等、网络/写入/checkpoint/新产物全 0；授权一次消费不可重放）。
E1/C1 未重跑。第一次 R3D2 发现的两个精确守卫缺口由 `d95db32` 修复，R3D2-R2 已于
2026-08-18 独立复验通过；Stage 1 完成。详见
[实施记录](S1-08R3_IMPLEMENTATION.md)、[最终验收记录](S1-08_ACCEPTANCE.md)与 ADR-028。
