# Stage 1 简明交接

## 当前结论

- 当前目标数据源已改为 AKShare；上游分别为东财、腾讯和新浪。
- S1-01～S1-06 保留，旧 S1-07A～C1C1-P0 的实现、专用测试和当前文档已删除。
- 旧路线真实数据运行数为 0，因此删除不会丢失已发布真实数据能力。
- Alembic 0010～0013 作为历史升级链保留；K1 删除 active assessment 模型和运行时依赖，不再创建新的 assessment 记录。未经备份和明确迁移任务，不删除既有数据库中的历史字节。
- S1-07K1-D1 已执行唯一东财直连复验：固定同窗口两只证券各尝试一次、零重试，均为 52 upstream/41 Canonical、`complete=True`；marker 1 passed，failure count=0，代理环境已恢复。结合 R1 腾讯/Sina 四组 complete，K1 功能验收通过。D1 后完整质量门 935 passed，全部固化门禁全绿；R2 的 922 passed 仅作历史基线。当前变更未提交且工作树不干净，K2 未开始。
- `SourceOverride` 与 EOD evidence 是 K2 前的过渡实现，不得继续扩展；K2 分别以 `ResolutionDecision` 和最小恢复事实替换。
- Stage 1 未完成；Stage 2 未授权、未开始。

## 当前交付点

[S1-07K1](S1-07K1_TASK.md) 停在 D1 功能验收通过交付点。R2 代理路径两条网络失败、脱敏 network reason 合同和 D1 两组直连 complete 证据详见 [K1 实现交接](S1-07K1_IMPLEMENTATION_HANDOFF.md)。所有真实授权均已消耗，禁止重跑；K2 不会自动开始，须等待用户明确授权。

继续时先阅读 `AGENTS.md`、`docs/DECISIONS.md`、本文件、K1 任务卡和 [K1 实现交接](S1-07K1_IMPLEMENTATION_HANDOFF.md)。不得恢复旧 S1-07 密码学证据链；即使 K1 功能验收已经通过，未获用户明确授权仍不得开始 K2。

K1 完成后唯一入口是 [S1-07K2 任务卡](S1-07K2_TASK.md)；不得从历史 S1-04/S1-05 验收文档恢复旧 override 或 evidence 合同。

## 为什么不回退 Git

如果只想查看旧 S1-07 开始前的代码，基线是提交
`44d268b3db8bd9c690225f62e60fa12bf56a289d`（S1-06 主验收）。

不建议对主分支执行 `git reset --hard 44d268b`：那会丢掉之后的审计历史，并让可能已迁移到 0012 的开发数据库出现版本断链。当前清理提交等价于“保留历史的逻辑回退”，同时增加 0013 兼容迁移。
