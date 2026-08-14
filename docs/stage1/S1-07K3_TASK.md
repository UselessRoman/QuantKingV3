# S1-07K3 历史任务记录：BaoStock、四角色基线与迁移框架

## 状态

历史实现已提交：`38103c1`、`31419bc`、`648890e`。本卡不再承载待开发工作；后续来源、
实测和迁移全部转入 [S1-07K4](S1-07K4_TASK.md)。

K3A～K3D 曾实现 BaoStock A、东财 B、腾讯 C、 新浪 D 的四角色基线、Decimal 差异证据、
`XTQUANT_LEGACY / XTQUANT` 的 `DATA_ONLY` 迁移框架、EOD、DataCenter 与只读 verifier。
该基线有合成回归，但不代表 ADR-021 的三角色目标已经实现。

## 不可变历史事实

- `baostock==0.9.3` 已精确落锁并建立适配器、Raw、单位、错误和 Canonical 合同。
- Stage 0 只读元数据记录旧根候选 5,208 个文件、527,708,270 字节；实际 inventory、pilot、
  full import 尚未完成。这两个数字不能冒充迁移结果。
- 2026-08-12 首轮探针只完成 BaoStock/`000001.SZ` 1/8 次，随后东财失败并停止。
- 2026-08-13 代表性四源计划三次在第 2 次东财调用失败；腾讯/新浪最小连通性成功。东财 IPv6
  和限流诊断由已退役 ADR-019 及 evidence 保留。
- FTShare R1～R3 和资格验收由 ADR-020 拒绝；证据保留，临时探针代码不进入生产。
- 所有真实探针应用层零自动重试、失败后停止；历史授权已经结束。

## 已交付但被取代的基线

```text
BAOSTOCK / BAOSTOCK
AKSHARE / EASTMONEY
AKSHARE / TENCENT
AKSHARE / SINA
XTQUANT_LEGACY / XTQUANT
```

历史 migrations、Raw、DifferenceReport、Resolution 和失败证据必须继续可读，不能因角色重排
而改写。新的正式 Job 不得调用东财或 FTShare。

## 后继任务

- [S1-07K4](S1-07K4_TASK.md)：实现 ADR-021 三角色路线、修复仲裁/质量、完成代表性实测和旧
  日线实际迁移。
- [S1-08R2](S1-08R2_TASK.md)：发布引擎离线收口。
- [S1-08R3](S1-08R3_TASK.md)：唯一正式 Job 与 Stage 1 最终验收。
