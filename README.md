# QuantKing V3

QuantKing V3 是单用户、本机运行的 A 股日频量化研究项目。当前只做可靠日线数据、可复现研究和人工调仓，不做自动下单、分钟/Tick、高频、多用户或云服务。

## 当前数据路线

- 目标路线固定为 BaoStock(A) 常态供数 + AKShare/腾讯(B) 常态互验；异常范围调用
  AKShare/新浪(C)。
- 三个实际来源分别落 Raw；至少两个实际持有同一行的独立来源在冻结规则内一致才可发布。
- 旧项目 XTQuant 日线只按 `DATA_ONLY` 受管导入，并逐行取得 BaoStock/AKShare 独立复核；
  不恢复 XTQuant、MiniQMT 或 QMT 运行依赖。
- 每个证券/交易日的选择来源和原因由 `ResolutionDecision` 追溯；无两源一致时隔离。
- S1-07K3 的 BaoStock 适配器、历史四角色仲裁、受管旧数据导入器、正式 EOD、DataCenter 与
  只读 verifier 已完成离线实现；该基线尚未重构为现行三角色路线。

```text
BaoStock(A) + AKShare/Tencent(B) + AKShare/Sina(C) + XTQuant legacy DATA_ONLY
    → Raw
    → Canonical
    → 质量与跨源比较
    → 冲突隔离或原子发布
    → Qlib/因子/模型/回测（后续 Stage）
```

## 项目边界

- Python 3.12，`uv` 管理依赖。
- Parquet 保存批量数据，DuckDB 查询，SQLite 保存元数据和任务。
- Stage 1 生产 EOD 只处理 `daily_bar_unadjusted`，不为其他数据集预建运行分支。
- FastAPI + React 仅监听 `127.0.0.1`。
- Qlib 用于研究，Backtrader 用于正式回测验收。
- 代码根：`E:\QuantKingV3`；运行数据根：`E:\QuantKingV3Data`。

## 状态

Stage 0 已完成。Stage 1 的 S1-01～S1-06、K1 和 K2 已完成主验收；S1-08 首次正式 Job 因
量额零容差被阻断，没有发布版本。K3 四角色离线基线已完成；东财与 FTShare 已分别由
ADR-021、ADR-020 退出目标路线。Stage 1 现在只按 `S1-07K4 → S1-08R2 → S1-08R3`
顺序收口；旧根尚未读取。Stage 1 尚未完成，Stage 2 未开始。

- [总体架构](docs/ARCHITECTURE.md)
- [架构决策](docs/DECISIONS.md)
- [实施计划](docs/IMPLEMENTATION_PLAN.md)
- [Stage 1 当前入口](docs/stage1/README.md)
- [路线图](docs/ROADMAP.md)

默认质量门：

```powershell
.\scripts\quality.ps1
```

定向开发命令统一通过仓库入口运行，避免依赖当前 PowerShell 的 PATH：

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
