# Stage 1 简化收口基线

本文件替代旧的 S1-07 证据合同交接。后续模型只需遵循这里的产品目标、任务顺序和边界。

## 产品目标

为单用户、本机 A 股研究提供可靠的未复权日线：

1. 取得数据；
2. 多上游核对；
3. 保留来源和版本；
4. 冲突时安全失败；
5. 发布可复现 DatasetVersion。

“安全”指只读采集、无交易接口、Raw 只追加、显式来源、冲突隔离、失败不发布和无前视，不指机构级不可抵赖审计。

## 数据源策略

| 角色 | provider | upstream | AKShare 接口 | 调用时机 |
|---|---|---|---|---|
| A | AKSHARE | EASTMONEY（东财） | `stock_zh_a_hist` | 每次 |
| B | AKSHARE | TENCENT（腾讯） | `stock_zh_a_hist_tx` | 每次 |
| C | AKSHARE | SINA（新浪） | `stock_zh_a_daily` | A/B 异常时 |

首期统一使用未复权日线。复权、财务、分钟/Tick、实时行情和账户数据不进入本轮。

## 仲裁原则

1. 三个上游先独立进入 Raw。
2. A/B 均成功且在阈值内一致：选择 A。
3. A/B 任一失败、缺行、质量失败或分歧：调用 C。
4. 三源中任意两源一致：从一致集合按 A、B、C 优先级选择。
5. 找不到两源一致：该证券/日期隔离，不产生 Canonical 发布候选。
6. 通过 `ResolutionDecision` 记录选择的 upstream、参与比较的 upstream、规则版本、决定原因和差异报告。

阈值不得在文档中凭经验拍定。K1 用真实小样本确认精度和单位，K2 再把阈值冻结为版本化配置。

## 保留与退役

保留：S1-01～S1-06 的 Raw/Canonical/Warehouse、质量比较、Job/Worker、原子发布、数据中心和对应通用测试。

K1 退役：XTQuant/QMT 生产适配与配置、旧来源枚举、`SourceCandidateAssessment`、24 单元矩阵和非日线 EOD 分支。

K2 退役：`SourceOverride` active 工作流、平行 success/failure evidence 和多层 checkpoint 恢复。Git 历史与既有数据库字节仍可审计，不在未授权数据根上执行破坏性清理。

过渡：S1-03 XTQuant 适配器只因当前 EOD runtime 仍引用而暂留；K1 必须以 AKShare 替换后删除，不得继续扩展。

## 剩余顺序

```text
S1-07K1 三渠道适配、XTQuant 退役、小样本真实探针
    → 独立主验收
S1-07K2 `ResolutionDecision`、C 异常仲裁、旧 override/evidence 退役、生产 EOD
    → 独立主验收
S1-08 明确范围真实日线发布
    → Stage 1 最终验收
```

任何真实网络探针和正式数据根写入仍按任务取得一次明确授权。一次 K1 授权覆盖任务卡列明的固定小样本调用，不需要每个 HTTP 请求再生成授权文件；超出样本、重试失败任务或扩大数据范围需要重新确认。
