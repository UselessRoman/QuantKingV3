# QuantKing V3

QuantKing V3 是单用户、本机运行的 A 股日频量化研究项目。当前只做可靠日线数据、可复现研究和人工调仓，不做自动下单、分钟/Tick、高频、多用户或云服务。

## 当前数据路线

- 统一使用 AKShare。
- A=东财、B=腾讯：每次日线更新互相校验。
- C=新浪：A/B 任一失败、缺失或分歧时参与第三方判定。
- 三个上游分别落 Raw，Canonical 每行记录最终选择来源和原因。
- 无法得到两源一致时隔离，不发布。
- 不依赖 XTQuant、MiniQMT、QMT 客户端、券商账号或密码。

```text
AKShare(Eastmoney/Tencent/Sina)
    → Raw
    → Canonical
    → 质量与跨源比较
    → 冲突隔离或原子发布
    → Qlib/因子/模型/回测（后续 Stage）
```

## 项目边界

- Python 3.12，`uv` 管理依赖。
- Parquet 保存批量数据，DuckDB 查询，SQLite 保存元数据和任务。
- FastAPI + React 仅监听 `127.0.0.1`。
- Qlib 用于研究，Backtrader 用于正式回测验收。
- 代码根：`E:\QuantKingV3`；运行数据根：`E:\QuantKingV3Data`。

## 状态

Stage 0 已完成。Stage 1 的 S1-01～S1-06 已完成主验收；旧 S1-07 机构级证据链已经退役。唯一下一任务是
[S1-07K1：AKShare 三渠道适配与小样本实测](docs/stage1/S1-07K1_TASK.md)。

- [总体架构](docs/ARCHITECTURE.md)
- [架构决策](docs/DECISIONS.md)
- [实施计划](docs/IMPLEMENTATION_PLAN.md)
- [Stage 1 交接](docs/stage1/HANDOFF.md)
- [路线图](docs/ROADMAP.md)

默认质量门：

```powershell
.\scripts\quality.ps1
```

真实网络测试和正式数据根写入必须由用户按任务明确授权；默认测试完全离线。
