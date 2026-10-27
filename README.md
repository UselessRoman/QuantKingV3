# QuantKing V3

QuantKing V3 是单用户、本机运行的 A 股日频量化研究项目。系统当前提供已验收的未复权日线
采集、跨源核对、版本发布、查询与任务追踪；研究、模型、组合和回测能力按后续 Stage 逐步交付。

## 当前能力

```text
BaoStock A + 腾讯 B → 独立 Raw 与质量检查 → 版本化比较
                                      ├─ 一致：选择 A
                                      └─ 异常：受影响范围调用新浪 C
无两个独立来源一致 → 隔离，不发布
```

- 唯一生产行情数据集是沪深 A 股未复权日线 `daily_bar_unadjusted.v1`。
- 每个发布行可追溯实际来源、差异、仲裁决定、质量结果和父血缘。
- Raw 只追加；已发布版本不可原地修改；失败任务不发布半成品。
- 旧 XTQuant 日线评估已按 ADR-027 以“迁移拒绝”收口，不进入 Raw、Canonical 或 Warehouse。
- 系统不连接 QMT、券商账号或交易 SDK，不提供自动下单。

## 项目状态

阶段状态、权威验收与下一步只在 [路线图](docs/ROADMAP.md) 维护；执行细节与历史失败保留在
对应 Stage 的实施和验收记录中。

## 技术边界

- Python 3.12 与 `uv`；Parquet 批量存储、DuckDB 查询、SQLite 元数据和任务。
- FastAPI + React 只监听 `127.0.0.1`。
- Qlib 用于研究，Backtrader 用于正式回测验收。
- 代码根：`E:\QuantKingV3`；运行数据根：`E:\QuantKingV3Data`。

## 文档入口

- [架构决策](docs/DECISIONS.md)
- [实施计划](docs/IMPLEMENTATION_PLAN.md)
- [总体架构](docs/ARCHITECTURE.md)
- [数据架构](docs/DATA_ARCHITECTURE.md)
- [Stage 1 历史索引](docs/stage1/README.md)
- [Stage 2 规划入口](docs/stage2/README.md)
- [Stage 3 模型规划入口](docs/stage3/README.md)
- [Stage 4 组合回测规划入口](docs/stage4/README.md)
- [Stage 5 调仓对账规划入口](docs/stage5/README.md)
- [Stage 6 系统验收规划入口](docs/stage6/README.md)
- [Stage 7 按需数据扩展入口](docs/stage7/README.md)

默认完整离线质量门：

```powershell
.\scripts\quality.ps1
```

定向命令统一通过仓库入口运行：

```powershell
.\scripts\uv.ps1 run --offline --frozen pytest tests\test_development_tooling.py -q
.\scripts\npm.ps1 --prefix frontend run typecheck
.\scripts\git-read.ps1 status --short
```

也可以先用 `. .\scripts\activate-toolchain.ps1` 激活当前 PowerShell，再直接调用 `uv`、
`node` 和 `npm`。固定工具链根是 `E:\Apps\QuantKingV3Toolchain`；不得改用 Codex 内置
Node 或历史的 `E:\Apps\uv\uv.exe`。共享 uv cache 受当前执行环境 ACL 阻断时，使用
`.\scripts\quality.ps1 -UvCacheDirectory .uv-cache` 执行同一离线质量门。

Codex 的只读 Git 命令使用 `scripts\git-read.ps1`；Git 元数据写入必须通过明确授权在
沙箱外执行，不能通过删除不存在的 `index.lock` 或修改仓库 ACL 绕过。

真实网络测试和正式数据根写入必须由用户按任务明确授权；默认测试完全离线。
