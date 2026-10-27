# Stage 3 文档入口

Stage 3 使用 Stage 2 显式研究版本训练固定基线，发布可复算预测，并批准一个可进入 Stage 4
正式组合回测的候选模型。阶段状态只看 [路线图](../ROADMAP.md)。

## 文档职责

- [任务索引](TASKS.md)：S3-01～S3-03 的顺序、共同边界和后移项。
- [S3-01 任务卡](S3-01_TASK.md)：时间切分、对照标签与实验合同。
- [S3-02 任务卡](S3-02_TASK.md)：Ridge/LightGBM 基线与预测版本。
- [S3-03 任务卡](S3-03_TASK.md)：单次 Alpha158 基准、最小页面与候选批准。
- [验收矩阵](ACCEPTANCE.md)：阶段门禁与证据要求。

## 阶段边界

- 所有实验绑定显式 `ResearchInputVersion`、`UniverseVersion`、`LabelVersion`、
  `FactorSetVersion` 和 `QlibViewVersion`，不读取隐式 `latest`。
- 只实现 Ridge 与 LightGBM 两个固定基线；Alpha158 只运行一个冻结比较实验。
- 所有可学习预处理只在训练区间拟合；标签、未来状态和测试期统计不得进入特征。
- Stage 3 只报告数据/分数层指标，不计算 Top-N 组合收益、回撤、换手或成本。
- 模型状态使用简单枚举和审计；本阶段最高到 `已批准`，不得设为 `使用中`。
- 不建设超参数搜索、自动重训、模型注册平台、多级审批或复杂实验仪表盘。

## 主链

```text
Stage 2 QlibViewVersion
  → 时间切分 + 训练区间预处理
  → Ridge / LightGBM ExperimentRun
  → PredictionSet + 模型 artifact
  → 单次 Alpha158 基准
  → 用户批准一个候选模型
```
