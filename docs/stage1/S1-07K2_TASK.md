# S1-07K2 任务指令：三渠道仲裁与简化生产 EOD

## 状态

纯离线实现完成并通过独立主验收。所属 Stage：Stage 1。没有运行真实网络、写正式数据根
或开始 S1-08/Stage 2；实现证据见 [K2 实现交接](S1-07K2_IMPLEMENTATION_HANDOFF.md)。

## 目标

把 K1 已验证的三个 AKShare upstream 接入同一条日常 EOD：A/B 常态互验，异常时调用 C，以不可变 `ResolutionDecision` 记录正常选源；同时退役旧 `SourceOverride` 和重复 EOD evidence 控制面。

## 输入边界

- 数据集只允许 `daily_bar_unadjusted`。
- A=`EASTMONEY`、B=`TENCENT`、C=`SINA`。
- K1 已冻结字段、单位、日期和错误合同。
- 比较阈值必须由 K1 真实小样本形成版本化配置，不得凭经验新增。
- 默认开发和测试离线；任何真实网络或正式数据根写入仍须按任务明确授权。

## ResolutionDecision

正常仲裁不使用人工审批。每个决定至少绑定：

- `symbol`、`trade_date`；
- 三个 upstream 对应的 batch/content SHA；
- `compared_upstreams`、`agreeing_upstreams`、`selected_upstream`；
- `reason_code`、`rule_version`、`difference_report_id`；
- 创建 Job 和时间。

允许把全版本共同的默认决定写在 resolution run 摘要中，只为缺行、分歧或非默认来源保存行级例外，避免重复存储相同文本。决定作为不可变 artifact 保存并由 DatasetVersion manifest 绑定；SQLite 只保存摘要、相对路径和 SHA，不保存全市场逐行矩阵。

## 仲裁流程

1. A/B 分别采集、Raw 落地并通过单源质量。
2. A/B 行级一致时选择 A，不调用 C。
3. A/B 任一失败、缺行、质量失败或超过阈值时，仅为受影响范围调用 C。
4. 任意两源一致时，在一致集合内按 A、B、C 优先级选择。
5. 无两源一致的证券/交易日进入冲突 artifact，不进入发布候选。
6. 发布候选、resolution artifact、DifferenceReport 和 DatasetVersion manifest 相互绑定。

## 退役 SourceOverride

- 从 active domain、service、repository、API、Web 和运行时删除申请、批准、拒绝、claim、attempt、child-version 与恢复逻辑。
- 数据中心把 Override 页面替换为 resolution run、来源例外和冲突隔离视图。
- 历史 Alembic 迁移和既有数据库字节保留；默认运行时不再读取或写入。物理删表或迁移压缩必须另立有备份的维护任务。
- 未来确需人工改判时，必须新建任务和 DatasetVersion，不恢复旧审批状态机。

## 简化 EOD 恢复

权威事实限定为：冻结 `JobRun`、Raw manifest、resolution/difference artifact、DatasetVersion manifest 和 JSON Lines。

- 进程中断后旧 Job 终态为 `INTERRUPTED`，不原地继续。
- 用户重试创建新 Job，并保留对旧 Job 的关联。
- 入队输入指纹绑定规范命令、代码版本、阈值/规则版本和明确数据范围。
- 若输入指纹已对应完整 `PUBLISHED DatasetVersion`，复验 manifest/`COMPLETE` 后直接复用。
- 否则从安全业务边界重新执行。
- 删除 active `EodEvidenceWriter`、success/failure evidence、forensic/authoritative 双终态和多层 checkpoint 协调；不得创造等价的新名字重新引入。

## 测试与验收

- A/B 一致时 C 零调用。
- A/B 各类失败、缺行、质量失败和字段分歧按受影响范围调用 C。
- 三种“两源一致”和三源无共识均有确定性测试。
- resolution 默认摘要与例外 artifact 可重复生成，manifest/SHA 完整绑定。
- 无共识、质量失败或 artifact 不完整均阻止发布。
- 中断 Job 关闭、新 Job 重试、已发布指纹复用和 manifest 损坏拒绝均有集成测试。
- active 代码中不存在 `SourceOverrideService`、旧 assessment 或 EOD evidence writer；历史迁移和验收文档命中必须逐项解释。
- 数据中心展示 resolution、差异和冲突，不显示旧 override 工作流。
- 完整离线质量门、前端、迁移、Markdown 链接和 `git diff --check` 通过。

## 明确不在范围

- S1-08 全市场/正式数据根发布。
- DataCenter 日常读取与完整 SHA 深验的 P1 重构。
- Stage 2 因子、复权、动态股票池。
- Stage 3 模型生命周期简化。
- Stage 4 双回测 ADR 复核。
- 券商 SDK、自动下单、分钟/Tick、调度器或机构级证据链。
