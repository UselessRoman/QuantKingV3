# S1-07K2 实现交接

## 结论

S1-07K2 的纯离线实现已完成并通过独立主验收。Stage 1 尚未完成；本轮没有运行
`real_akshare`、没有写正式数据根，也没有开始 S1-08 或 Stage 2。工作树未提交。

## 已实现主链

- 日线比较阈值冻结为
  `comparison-thresholds.daily-bar-unadjusted.k1.v1`；OHLC、`pre_close`、成交量和
  成交额的 absolute/relative 容差均为 0。
- A=东财、B=腾讯正常独立采集、Raw 落地和单源质量；A/B 完全一致时选择 A，C
  零调用。失败、质量失败、缺行或字段差异只触发受影响证券和日期 span 的 C=新浪。
- AB、AC、BC 任意两源一致均可形成决定；一致集合按 A、B、C 选择。存在性共识可
  选择“缺失行”并从候选中省略；三源无共识写冲突并阻止发布。
- 不可变 `ResolutionRun`/`ResolutionDecision` artifact 使用默认摘要加行级例外，绑定
  输入批次、Raw content SHA、DifferenceReport、创建 Job、规则版本和 C 实际请求范围。
  SQLite 0014 只保存 run 摘要、相对路径、SHA 和计数，不保存逐行矩阵。
- DifferenceReport K2 v2 允许两个单源质量结果绑定同一 BUILDING final candidate；实际
  A/B/C 批次全部确定后才创建该候选。最终 manifest 自包含绑定 Raw batch 清单、每份
  DifferenceReport 的路径/SHA 和 resolution artifact。
- 相同输入指纹只在 PUBLISHED DatasetVersion、Raw、DifferenceReport、resolution、
  manifest 和 `COMPLETE` 全部深验通过时复用；任一篡改稳定拒绝。失败或中断只关闭本
  Job 的 BUILDING/VALIDATED 候选，不改变 PUBLISHED 或其他 Job 版本。

## 控制面退役与 DataCenter

- active `SourceOverride` domain/service/repository/API/Web/UI 以及申请、审批、claim、
  attempt、child-version、recovery 已删除。历史 0003/0005/0007/0008/0009 迁移和 ORM
  只读映射保留，以兼容历史数据库字节。
- active `EodEvidenceWriter`、success/failure evidence、forensic/authoritative 双终态、
  `.checkpoints` 发布恢复和旧 EOD result 合同已删除。通用 Worker safe checkpoint/cancel
  保留；重试用 `retry_of_job_id` 审计关联且必须冻结同一输入。
- DataCenter 增加 resolution run 列表、bounded detail、来源例外和冲突分页 API/UI。
  detail 不加载全市场逐行数据；两个列表维护独立 cursor，并能继续加载第 51 行以后。

## EOD 回归迁移分类

原 S1-05 EOD 专用测试没有简单以少量 K2 happy-path 替代。K2 回归保留并适配了：

- daily-only 前置拒绝且零 gateway、partial/单源失败隔离、final quality
  FAIL/SKIP/exception 净化与不发布；
- 冻结 command、base credential、Raw/Difference/resolution/manifest/`COMPLETE` 篡改拒绝；
- base 历史不可变、目标窗口权威替换、缺行删除和越界 inventory 拒绝；
- 取消 safe checkpoint、failed/interrupted 新 Job retry、stale recovery 幂等且不碰其他
  Job/PUBLISHED、发布后故障保留 PUBLISHED；
- formal runtime 三 upstream 组合、错误 revision、Web/CLI fresh lease、严格 published
  fingerprint 复用。

旧 coverage-only `REUSED_UNCHANGED` 被 K2 的严格相同 fingerprint+完整 PUBLISHED 复验
取代。只属于旧 evidence/forensic/checkpoint-inventory/self-signed evidence 的用例随对应
生产控制面删除。

## 本地验证

- `pytest --collect-only -q`：最初 895 collected、无 import/collection error；增加最终状态断言后
  当前为 896 collected。
- 最终完整离线 marker：889 passed、2 skipped、5 deselected、4 warnings，132.95s。
- K2 resolution/EOD/DataCenter/Web 聚焦：91 passed；EOD 单文件 32 passed。
- Ruff、format、mypy 111 source files、前端 typecheck、17 unit、production build 全绿。
- Playwright：1 passed；E2E fixture 使用显式 shutdown，命令在 9.3s 内正常退出。
- 历史 migration 0003/0005/0007/0008/0009 diff 为 0；`git diff --check` 通过。

## 独立主验收

- 锁文件离线检查解析 222 packages，frozen sync 检查 216 packages。
- 完整离线 marker：889 passed、2 skipped、5 deselected、4 warnings，131.62s；K2 组合
  聚焦 122 passed，EOD 单文件 32 passed。
- Ruff、194 files format check、mypy 111 source files、架构边界、23 个文档状态测试、
  Markdown 链接和 `git diff --check` 全绿。
- 前端 typecheck、17 unit、production build 与 Playwright 1 passed（6.4s，Web server
  正常关闭）全绿。
- 固化 `scripts/quality.ps1` 首步受共享 `E:\Apps` UV cache Windows ACL 阻断；主验收改用
  仓库忽略的 `.uv-cache` 逐项执行同等门禁并记录真实退出码。

K2 独立主验收通过，但不得据此宣称 Stage 1 完成，也不得自动开始 S1-08。

## 已知边界

- 本轮没有网络授权，未重跑 K1 已消耗的真实探针；K2 依赖 K1 已冻结的三源合同。
- 没有正式数据根 DatasetVersion；首个明确范围真实发布属于 S1-08，须另行授权。
- 历史 migration/历史验收文档中的 `SourceOverride`、evidence 字样是审计材料，不是 active
  runtime 合同。
