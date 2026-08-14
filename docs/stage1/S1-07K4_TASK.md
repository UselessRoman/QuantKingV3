# S1-07K4 任务卡：三角色数据路线与旧日线迁移

## 目标与边界

所属 Stage：Stage 1。状态：待开发。前置决策：ADR-020、ADR-021。

本任务把已提交的历史四角色实现迁移为唯一现行路线：

```text
BaoStock A + AKShare/腾讯 B → 常态互验
                         └→ 异常范围调用 AKShare/新浪 C
旧 XTQuant X → DATA_ONLY 受管导入 → 同一 ResolutionDecision
任意发布行没有两个实际来源一致 → 隔离，不发布
```

本任务拥有来源适配器、仲裁语义、数据质量、代表性实测、比较规则重基线和旧数据迁移。
不拥有 SQLite 发布恢复、正式 R2 Job 或 Stage 1 最终验收；这些分别属于 S1-08R2/R3。

## K4A：三角色实现重构

- 删除东财正式 runtime、registry、预算和新 Job 指纹分支；保留历史 Raw、migration 和证据读取。
- 把腾讯固定为 B、 新浪固定为 C；更新 Resolution、DifferenceReport、artifact/verifier、
  DataCenter、CLI/Web 投影和 provider-neutral 测试。
- 正常任务只计划 A/B；C 只按受影响证券/日期调用。A/B 一致时 C 的 planned/actual 都为 0。
- BaoStock、腾讯、 新浪分别记录 provider/upstream、endpoint、库版本、批次、质量和错误状态。
- 所有服务调用使用适配器层硬超时或受控子进程；应用层零重试，预算不足在首次调用前拒绝。
- lookback、单位策略、路由、阈值和代码版本进入可重放输入指纹。

## K4B：仲裁与质量语义

- 任一来源持有行时，其他来源的共同缺行不能形成值共识；恰好一条实际行最终必须隔离。
- 多源合法整范围空结果使用独立 zero-row 原因码，不能冒充逐行价格一致。
- DifferenceReport 与 Resolution 共用一个版本化 Decimal `ThresholdSet`。
- Canonical `pre_close` 对全部服务源统一由完整未复权序列的前一行 `close` 派生；vendor 参考
  昨收只保留 Raw。
- 引入绑定版本和内容 SHA 的交易日历；逐证券检查预期交易日、缺口、新鲜度、上市/停牌例外。
- Raw/质量失败保存稳定脱敏错误码、异常链、影响计数和有界行级证据。

## K4C：代表性三源实测与规则冻结

离线实现和完整门禁通过后，先输出 dry-run，再单独申请真实网络授权。样本至少覆盖普通、除权、
停牌、低流动性、量额边界、缺行/空结果和沪深不同代码段。

- 固定证券、target/lookback、每源 endpoint/HTTP 上界、硬超时、零重试和失败即停。
- 验证 BaoStock、腾讯、 新浪的字段、单位、未复权、日期、完整性、错误和实际差值。
- OHLC/`pre_close` 默认零容差；量额容差只能由代表性证据和边界测试支持。
- 新阈值、单位和路由版本冻结为不可变合同；历史 K1/K3 证据继续可复验。

一次小样本成功不证明全市场稳定性。失败必须保留证据，不能在同一授权下自动重跑或扩大范围。

## K4D：旧 XTQuant 日线 DATA_ONLY 迁移

清单、pilot、full 是三次独立授权，不得合并：

1. **只读 inventory**：扫描 `E:\QuantKing\data\kline\1d`，冻结相对路径、大小、mtime、
   SHA-256、schema、行数、代码/日期覆盖、空值、重复键和读取错误；旧根前后指纹一致。
2. **pilot**：固定少量代表性证券复制到受管 Raw/staging，确认字段、单位、未复权、停牌和
   `pre_close` 语义，与 A/B/C 复核；失败对象隔离。
3. **full**：全部候选导入或明确隔离，发现/读取/复制/转换/复核/隔离计数和字节守恒。

Stage 0 的 5,208 个文件、527,708,270 字节只是元数据基线，不得冒充实际成功迁移。旧文件不
修改、不硬链接、不启动旧项目、XTQuant 或 QMT。任何准备发布的历史行都必须得到两个实际
独立来源支持。

## K4E：验收与交付

- 锁文件、Ruff、format、mypy、全部离线 pytest、前端、E2E、Markdown、`git diff --check`
  全部通过。
- 三角色 topology、仲裁/质量、真实实测和旧迁移由独立 agent 验收。
- 形成一个独立 K4 提交和不可变验收记录；随后才能开始 S1-08R2 验收与 S1-08R3 正式执行。

## 授权边界

任务卡和离线开发不授权网络、读取旧项目、写 `E:\QuantKingV3Data`、访问 F 盘、启动 QMT/
XTQuant/券商或开始 Stage 2。每个真实步骤必须有独立 marker、固定范围、预算、零重试和失败
后停止条件。
