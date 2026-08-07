# S1-07C0B-R1 独立主验收记录

状态：**已完成独立主验收（纯离线 Fake provider）**。

## 结论与范围

独立主验收接受 S1-07C0B-R1 结果提交
`20c7aaba4de4a03b58ea148c4f56e8aec3eb2092`。本验收只覆盖仓库内、纯离线的 Fake-provider
受控执行、assessment seal 持久化和 DataCenter 读侧重验；Fake identity 永久推导为
`SYNTHETIC`。它不证明 REAL signer、REAL runner、XTQuant/QMT、网络或任一来源生产可用。

- 原任务基提交：`7d6bd44eecd3817fcd40b84f37697c166845686a`。
- R1 修复基提交：`5b461fc3be56ba6b864552fc927ff68c71ce9497`。
- 接受结果提交：`20c7aaba4de4a03b58ea148c4f56e8aec3eb2092`。
- 初版 `5b461fc3be56ba6b864552fc927ff68c71ce9497` 不作为最终结果；其阻断项已由 R1 复核关闭。

## 独立复核证据

1. 授权文件对所有 JSON 字段实施精确类型检查，拒绝未知字段和非 canonical 字节；解析后
   `to_bytes()` 必须与原 payload 完全一致。原签名下 integer→string、nested text→integer
   和其他类型归一化替换均被拒绝。
2. claim 前预检使用 S1-07B 的 authority bundle verifier、逐 case
   `AUTHORITY_APPROVED` 判定、包版本与 runner readiness。`calendar_sh` 等
   `FRAMEWORK_ONLY` case 不能执行；预检事件顺序保持 `PRECHECK → CLAIM → IMPORT → OUTPUT`。
3. 正式 `build_eod_runtime()` composition root 注入与写侧一致的 v3 sealed reader。每次
   DataCenter assessment 读取均重验 authority bundle、trust context、seal 和 archive；未配置
   或不匹配的 REAL trust 继续失败关闭。
4. 独立 trust policy 从受控 key-id 推导 evidence kind；Fake key 无法借由包装、自报、重签或
   调用方 allowlist 变成 REAL。没有 REAL marker、REAL 私钥、默认 REAL key 或供应商 SDK
   进入本验收范围。
5. 成功、受控失败与异常路径保留不可覆盖 v3 evidence 或明确 incomplete failure；claim 在失败、
   异常和崩溃后仍被消费，既有 run/失败目录不能原地改写为成功。
6. assessment persistence 只从受管目录重新深验 seal，拒绝手工 verified DTO、`replace` 篡改、
   直接 SQL gate/status 伪造、路径穿越、附加/缺失文件、SHA 失配和 symlink/reparse。v2 sealed
   persistence 路径继续按明确版本分派保留。

## 测试与质量门

- C0B 定向与攻击回归：`22 passed`。
- C0A 攻击回归：`21 passed`；S1-07B final：`46 passed`。
- `tests/data/validation`：`229 passed`；assessment persistence：`29 passed`；DataCenter：
  `31 passed`；migration：`52 passed`。
- 完整离线 pytest：`1272 passed, 2 skipped, 3 deselected, 4 warnings`。
- `scripts/quality.ps1` 完整通过：锁文件、Ruff、格式、mypy、pytest、架构边界、Markdown 链接、
  前端单测/Playwright/生产构建和 `git diff --check` 均通过。
- Alembic 唯一 head 为 `20260729_0012`；C0B 不新增机器可查询字段或状态，故未新增 migration，
  历史 `0012` 未修改。

## 副作用与后续边界

REAL marker 执行数为 0。未访问网络、XTQuant、QMT、任何供应商 SDK、
`E:\QuantKingV3Data`、`E:\QuantKing` 或 `F:\QuantKingV3Backups`；未创建真实密钥或 REAL
生产身份。

C1、真实 XTQuant/QMT/网络、正式数据根、S1-07D、S1-08、Stage 2 均未授权、未开始。本验收不
改变任何 source×dataset 的生产可用结论：24 个单元仍为 `BLOCKED_EXTERNAL_VALIDATION`，
真实运行数为 0。
