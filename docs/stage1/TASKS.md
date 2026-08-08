# Stage 1 任务卡

## 状态基线

| 任务 | 状态 | 保留结论 |
|---|---|---|
| S1-01 | 已主验收 | 数据 schema、元数据和安全迁移 |
| S1-02 | 已主验收 | Raw/Canonical/Warehouse、原子发布、显式版本查询 |
| S1-03 | 历史验收；待替换 | XTQuant 离线适配和增量抓取合同，不再是目标数据源 |
| S1-04 | 已主验收 | 质量、跨源比较、冲突报告和显式 override |
| S1-05 | 已主验收 | EOD、Worker、CLI/API、幂等和失败恢复 |
| S1-06 | 已主验收 | 数据中心 Web |
| 旧 S1-07A～C1C1-P0 | 已退役 | 真实运行数始终为 0；实现、专用测试和当前文档已删除 |
| S1-07K1 | 未开始；唯一下一任务 | AKShare 东财/腾讯/新浪适配、XTQuant 退役和小样本实测 |
| S1-07K2 | 未开始 | 三渠道比较、仲裁、失败隔离和生产 EOD |
| S1-08 | 未开始 | 首个明确范围 DatasetVersion 与 Stage 1 最终验收 |

S1-01～S1-06 的具体证据见本目录对应的 `S1-0x_ACCEPTANCE.md`。旧 S1-07 历史留在 Git 中，不要求新任务维护其密码学合同或状态措辞。

## S1-07K1：AKShare 三渠道适配与小样本实测

完整、可复制到新对话的任务指令见 [S1-07K1_TASK.md](S1-07K1_TASK.md)。

目标：

1. 将生产行情边界从 XTQuant 改为 AKShare。
2. 分别实现东财 `stock_zh_a_hist`、腾讯 `stock_zh_a_hist_tx`、新浪 `stock_zh_a_daily` 的日线适配。
3. 统一映射未复权日线，保留 provider/upstream/endpoint/AKShare 版本和采集时间。
4. 用少量沪深样本做一次经授权的只读真实网络实测，确认字段、单位、日期范围和错误行为。
5. 删除 S1-03 遗留的 XTQuant/QMT 生产适配、配置、兼容探针和专用测试；通用 ingestion/EOD/Raw/Warehouse 代码保留。

K1 不实现全市场下载、不写正式数据根、不发布 DatasetVersion、不做自动 fallback。

## S1-07K2：三渠道比较与日常 EOD

依赖：K1 已独立主验收。

- 正常采集 A=东财、B=腾讯并比较。
- 仅当 A/B 任一失败、缺行、质量失败或超过阈值时采集 C=新浪。
- 任意两源在冻结阈值内一致时，按 `EASTMONEY > TENCENT > SINA` 从一致集合中选择。
- 无两源一致时写冲突报告并隔离，不产生发布候选。
- 每个 Canonical 行记录选择来源和决定原因；三个 Raw 永远独立保存。
- 用限速、超时和有界重试保护公开上游；默认测试继续完全离线。

K2 不做分钟/Tick、实时行情、交易账户、自动下单或机构级证据链。

## S1-08：首个日线版本与最终验收

依赖：K1、K2 均完成独立主验收。

- 用户明确授权后写 `E:\QuantKingV3Data`。
- 构建一个事先声明证券范围和日期范围的未复权日线 DatasetVersion。
- 验证覆盖、重复、OHLC 约束、成交量/成交额、A/B/C 仲裁、失败隔离、原子发布和 Web 查询。
- 明确报告北交所、退市证券、复权和历史状态的未覆盖项。
- 完成后才可声明 Stage 1 完成；Stage 2 仍需单独授权。

## 已取消事项

- XTQuant/MiniQMT/QMT 行情路线。
- C1C1-P1-REVIEW、C2/C3/C4 等旧 S1-07 任务链。
- Ed25519、key-id allowlist、nonce/claim、plan→authorize→review→execute、seal 深验和 v1/v2/v3 证据分派。
- BaoStock、Tushare、JQData 作为 Stage 1 生产依赖。
- 17 类权威 purpose 和 24 个 source×dataset 机构级验证矩阵。
