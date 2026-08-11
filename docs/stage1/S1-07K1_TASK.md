# S1-07K1 任务指令：AKShare 三渠道适配与小样本实测

## 状态

S1-07K1-D1 唯一东财直连复验已执行：相同窗口的两只证券各尝试一次、零重试，均为 52 upstream/41 Canonical、`complete=True`，failure count=0。结合 R1 腾讯/Sina 四组 complete 与全套合同，K1 功能验收通过；D1 后完整门禁 935 passed，全部固化门禁全绿。所有真实授权均已消耗，严禁重跑；K1 已提交为 `f98f1d9`。K2 后续经用户明确授权并已独立主验收；S1-08 与 Stage 2 仍未获授权。

## 用户授权原文建议

新对话中由用户发送：

> 授权执行 S1-07K1：允许为任务卡固定的小样本访问 AKShare 的东财、腾讯、新浪公开日线接口；不启动或连接 QMT，不使用账号密码，不写 E:\QuantKingV3Data，不访问旧项目或 F 盘，不开始 K2、S1-08 或 Stage 2。失败后不要自动扩大范围。

没有这段或等价明确授权时，只能完成离线代码和测试，不能运行真实网络 marker。

## 基线与目标

- 基提交：开始任务时的当前 clean HEAD，必须包含本重基线文档和迁移 `20260808_0013`。
- 目标：生产行情边界改用 AKShare，建立三个明确 upstream 的日线适配器，删除旧来源/assessment 控制面，把生产 EOD 收窄到未复权日线，并用固定小样本验证真实字段和单位。
- 结果状态只能写“实现完成、等待独立主验收”或“被真实上游阻断”；不得直接宣称 Stage 1 完成。

## 实现范围

1. 用 `uv` 添加并锁定一个明确 AKShare 版本。
2. 在 `data/adapters` 实现三个延迟调用适配器：
   - 东财：`stock_zh_a_hist`；
   - 腾讯：`stock_zh_a_hist_tx`；
   - 新浪：`stock_zh_a_daily`。
3. 调用参数固定为未复权日线；证券代码格式转换集中在一个可测试模块。
4. 映射到既有 `daily_bar_unadjusted` schema。不得猜测成交量和成交额单位；必须由真实探针和文档共同确认。
5. Raw manifest 增加或复用字段：`provider`、`upstream`、`endpoint`、`akshare_version`、`fetched_at`、请求日期范围、响应 SHA 和 row_count。
6. 用 AKShare gateway 替换正式 EOD runtime 的 XTQuant gateway。
7. 完整删除 XTQuant/QMT 行情生产代码、配置、兼容探针、marker 和专用测试，至少覆盖：
   - `src/quantking/runtime.py` 的 XTQuant gateway 构造；
   - `configs/default.toml` 与 `common/config.py` 的 XTQuant 配置；
   - `data/models.py` 的 XTQuant/BaoStock/Tushare 生产来源枚举；
   - `data/adapters/sources/xtquant.py`、capability/probe 及专用测试；
   - `pyproject.toml`、`tests/conftest.py`、`scripts/quality.ps1` 的退役 marker。
8. 用最小 `DataProvider`、`DataUpstream` 和固定 source registry 替换旧来源模型；不得把三个 upstream 合并为一个来源。
9. 删除 active `data/assessment`、`SourceCandidateAssessment`、默认 24 单元矩阵、相关仓储方法和 DataCenter source readiness 读取。历史 Alembic 迁移和既有表字节只读保留，不在无数据根授权时执行破坏性清理。
10. 生产 EOD 运行时只接受 `daily_bar_unadjusted`。其他五类 schema 可以保留，但不得进入来源 capability、默认任务请求或 Worker 分支。
11. 保留 provider-neutral ingestion、Raw、质量、Warehouse、Job 和 Web 基础；不恢复任何旧 S1-07 validation/authorization/signature/seal 模块。
12. K1 不实现 `ResolutionDecision`、删除 `SourceOverride` 或重写 EOD evidence；这些属于 K2。现有过渡实现不得扩展新状态或合同。

## 固定真实探针

为了控制请求量，K1 只探测：

- `600000.SH` 和 `000001.SZ`；
- 最近 60 个自然日；
- 每个 upstream、每只证券最多一次调用；
- 无自动重试；
- 输出仅包含 schema、类型、行数、日期范围、单位结论、错误摘要和少量脱敏示例，不把大体量行情提交到 Git。

如某接口不支持相同日期窗口或代码格式，记录真实差异并安全失败，不通过额外接口偷偷替代。北交所在 K1 记为待验证缺口。

## 测试

- 三套合成响应的字段映射、排序、空结果和错误分类。
- provider/upstream/endpoint 血缘不能互换。
- source registry 精确包含 AKShare 的东财、腾讯和新浪，不存在 24 单元 source×dataset 矩阵。
- production EOD 对非 `daily_bar_unadjusted` 请求稳定拒绝，且不会调用任何 upstream。
- 默认 import、单元测试和完整质量门不访问网络。
- `real_akshare` 定向测试只能由上述授权显式运行。
- 正式 runtime 使用 AKShare gateway，且不再导入 `xtquant`。
- 既有 S1-01～S1-06 通用回归、Markdown 链接、迁移、前端和 `git diff --check` 通过。

## 验收交付

- 结果提交 ID、变更文件清单和工作树状态。
- 三渠道实际 AKShare 版本、endpoint、参数、行数、日期范围、字段/单位结论。
- A/B 同样本差异摘要；C 只证明可调用和可映射，K1 不实现仲裁。
- XTQuant/旧 assessment 删除清单与 `rg -n "xtquant|XTQuant|SourceCandidateAssessment|BLOCKED_EXTERNAL_VALIDATION"` 剩余命中解释；历史验收和 Alembic 文档中的文字可保留，但 active 生产代码不得残留。
- 完整质量门结果和未覆盖缺口。

## 明确禁止

- QMT/MiniQMT、券商账号、账户/交易 SDK 和下单。
- 全市场或全历史下载。
- 写正式数据根或发布 DatasetVersion。
- 自动 fallback、自动重试或超出固定样本。
- Ed25519、签名、nonce、claim、授权文件、seal、reference bundle 或攻击型证据测试。
- 开始 S1-07K2、S1-08 或 Stage 2。
