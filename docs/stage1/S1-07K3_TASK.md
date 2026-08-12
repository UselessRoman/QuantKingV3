# S1-07K3 任务卡：BaoStock 主供、四源仲裁与旧日线迁移

## 状态与目的

状态：设计已确认；K3A～K3D 离线实现完成。BaoStock 0.9.3、四源 A/B/C/D 仲裁、Decimal 证据、受管
`DATA_ONLY` inventory/import、正式 EOD、DataCenter 与只读 verifier 均有合成回归。唯一一次
真实 marker 已执行：BaoStock/`000001.SZ` 成功，随后东财网络失败，按零重试/失败即停规则
终止，余下六次未调用。代表性阈值证据和旧根实际迁移仍缺失，候选规则不得激活；旧项目
读取和正式数据根写入仍未授权。
所属 Stage：Stage 1。后续任务：[S1-08](S1-08_TASK.md)。

本任务解决两个已经实证的问题：

1. AKShare 三个 upstream 都依赖公开网站，不能单独承担长期常态供数可用性；新浪接口还不适合
   全市场常态抓取。
2. 旧项目 `E:\QuantKing` 已有一套约截至 2026 年 6 月的 XTQuant A 股历史日线，但未经独立
   来源复核，不能直接作为 V3 Warehouse。

K3 把 BaoStock 作为独立常态供数来源，保留 AKShare 东财、腾讯、新浪三个明确复核/仲裁
来源，并以 `DATA_ONLY` 方式导入、校验旧 XTQuant 日线。它不恢复 QMT/XTQuant 运行能力，
不迁移旧代码，也不开始 Stage 2。

## 已知输入与不得冒充的事实

- Stage 0 只读清单记录 `data/kline/1d/*.parquet` 共 5,208 个、527,708,270 字节；这只是文件
  元数据证据，不证明证券集合、最终交易日、字段、单位、复权或内容正确。
- 用户估计旧历史数据截至约 2026 年 6 月；K3 必须从文件内容生成实际 coverage 报告，不能把
  估计日期写成验收事实。
- S1-08 首次真实 Job 的价格字段一致，但三 AKShare upstream 的量额最大存在约 50 股/50 元
  级差异；历史零容差规则及失败产物必须保持不可变。
- BaoStock 0.9.3 已精确落锁并通过 Python 3.12 离线包/源码合同与合成适配器测试；固定
  marker 的一次真实 BaoStock 调用成功，但样本不足以证明代表性覆盖或跨源一致性。

## 固定来源与调用顺序

### 日常 EOD

| 顺序 | provider / upstream | 作用 | 调用条件 |
|---|---|---|---|
| A | `BAOSTOCK / BAOSTOCK` | 常态供数 | 每次 |
| B | `AKSHARE / EASTMONEY` | 常态独立互验 | 每次 |
| C | `AKSHARE / TENCENT` | 第一次异常仲裁 | A/B 失败、缺行、质量失败或分歧 |
| D | `AKSHARE / SINA` | 最后异常仲裁 | 调 C 后仍无两源一致 |

任何两源在冻结规则内一致即可形成决定；在一致集合内按 A、B、C、D 选择。D 只能访问仍未
解决的证券/日期范围，禁止作为全市场常态源。调用失败不得静默切源，无两源一致不得发布。

### 旧历史导入

旧文件使用 `XTQUANT_LEGACY / XTQUANT`，只表示静态历史来源，不表示 V3 可以调用 XTQuant。
每个 X 行先与 BaoStock A 比较：

```text
X 与 A 一致 → X 可形成已复核历史候选
X 与 A 不一致/缺失 → 仅异常范围调用 B，再按需要调用 C、D
仍无两个独立来源一致 → 隔离 X 行并报告
```

不得用“旧数据曾经可用”替代复核，也不得为了降低冲突而修改源值、覆盖 Raw 或粗粒度舍入
Canonical。若 BaoStock 不覆盖某市场、证券状态或历史区间，该范围进入显式缺口。

## 子任务

### K3A：BaoStock 依赖与适配器

- 使用固定工具链从包索引选择并精确锁定一个明确 BaoStock 版本；记录 wheel/sdist SHA、
  Python 3.12 导入结果、依赖、客户端许可证、数据使用条款和上游服务器地址/协议形状；
  数据使用条款不明确时必须列为发布风险，不能由客户端 BSD 许可证代替。
- 业务、研究、Web 和领域层不得导入 `baostock`；只在适配器层延迟 import、login、query、
  logout，普通模块导入、CLI `--help` 和 dry-run 均零网络。
- 集中转换证券代码、日期范围和 `adjustflag`。Stage 1 只允许未复权日线，未知频率、复权模式、
  字段或包版本在网络前拒绝。
- Raw 保存 BaoStock 原始字段和响应元数据；Canonical 映射必须真实确认 OHLC、`preclose`、
  `volume`、`amount`、交易状态、ST 标识、空值、停牌行和单位。
- BaoStock `preclose` 必须先确认是否为除权参考昨收；若与 Canonical“上一交易日未复权 close”
  语义不同，只保留在 Raw，并继续使用完整 lookback 中的前一行 close 派生 Canonical 值。
- 登录失败、查询失败、服务拒绝、超时/断连、空结果、部分结果、schema 漂移和 logout 异常
  使用稳定、脱敏的错误分类。应用层默认零自动重试。
- 为长连接和并发行为建立明确边界：一个 Worker 内的会话所有权、有界超时、必然 logout、
  不跨进程共享 socket，不在日志中泄露服务器地址或协议载荷。

### K3B：有界四源真实探针与比较规则重基线

真实探针必须另行授权，先 dry-run 冻结证券、日期、lookback、每源调用数和网络上限。首轮复用
`000001.SZ`、`600000.SH` 与 `2026-06-11..2026-08-09`，但这两只证券不足以永久冻结全市场
容差；随后还需一个经授权的代表性样本，至少覆盖：

- 沪市/深市不同代码段；
- 除权日、停牌日、ST、低成交量/低成交额；
- 接近 50 股/50 元的量额边界；
- BaoStock 或某 AKShare upstream 缺行/空结果；
- 若任务决定支持北交所或退市证券，必须作为独立 scope 验收，否则明确排除。

新规则版本必须满足：

- OHLC 和 `pre_close` 原则上零容差；任何例外需逐字段证据和新 ADR。
- 量额使用 `Decimal`/定点单位比较，不用二进制浮点决定阈值边界。
- DifferenceReport 永远保留各源原值、绝对/相对差、使用的单位策略和规则版本。
- 容差是“可判定一致”的规则，不是修改 Canonical 数值的规则。
- 缺行、单位不明、复权不明、质量失败和调用失败不受数值容差豁免。
- 历史 `comparison-thresholds.daily-bar-unadjusted.k1.v1` 与首次失败 Job 保持不可变。

当前离线实现只新增 `comparison-thresholds.daily-bar-unadjusted.k3-candidate.v1` 零值候选和
`difference-report-manifest.k3.v3` Decimal 字符串证据；候选显式 `active=false`。真实 marker
固定 8 次行情 endpoint（BaoStock 2、东财 2、腾讯 2、新浪 2），并把 BaoStock 协议消息上限
6 与 AKShare 锁包源码 HTTP 上限 12 分开报告。marker 必须经 Gateway 的 SupplierResponse 合同
和 Canonical normalizer 后比较，禁止把东财“手”等上游返回单位直接冒充 Canonical“股”。

2026-08-12，主 agent 按固定 marker 执行了唯一一次真实探针：第 1 个计划调用 BaoStock /
`000001.SZ` 成功；第 2 个计划调用 AKShare/东财时发生代理断连，任务按
`STOP_ON_FIRST_FAILURE` 立即停止，未重试，也未调用余下 6 个计划项。该结果只证明失败边界
生效并保留 BaoStock 单次成功事实；它不构成四源比较证据，不能激活或放宽 K3 候选阈值。

### K3C：旧 XTQuant `DATA_ONLY` 受管导入

当前仅完成纯离线框架和 `tmp_path` 合成 Parquet 验收：原生发现与冻结 inventory、逐文件
SHA/schema/coverage/null/duplicate/read_error、前后指纹、非硬链接受管复制、显式语义映射、
Canonical 候选、quarantine、manifest/COMPLETE、内容幂等和严格 verifier。候选状态固定为
`STAGED_X_ONLY_UNVERIFIED`，所有行（包括每证券首行无前序 close 的 `pre_close`）都仍是 X-only
未复核，不建立 Warehouse/PUBLISHED 版本。真实旧根清单、复制、pilot 和 full 均未执行，仍需
各自新授权。

旧项目读取和正式数据根写入必须另行明确授权。执行分为清单、试点、全量三次独立边界；任何
一次失败后停止，不自动重跑或扩大范围。

1. **只读清单**：原生递归 `E:\QuantKing\data\kline\1d`，记录全部相对路径、大小、mtime、
   SHA-256、Parquet schema、行数、代码/日期 coverage、空值/重复键和读取错误。前后源指纹
   必须一致；不启动旧项目、XTQuant 或 QMT。
2. **受管复制**：逐文件复制到 `E:\QuantKingV3Data` 的新 import Raw/staging 批次；使用临时
   文件、内容 SHA 校验和原子完成标记。V3 不把旧根当运行时数据目录。
3. **语义映射**：确认字段、代码、未复权、价格精度、成交量/成交额单位、时区和停牌/空值
   语义后生成 Canonical 候选。无法确认的文件隔离。
4. **小样本试点**：先用固定少量证券覆盖普通、除权、停牌、低流动性和边界日期；通过后才
   允许全量导入。
5. **全量复核**：按证券/日期分块与 BaoStock 比较，只对分歧/缺失范围调用 AKShare B/C/D；
   联网前输出精确计划和最坏调用预算并重新取得授权，禁止无上限抓取。
6. **发布候选**：只包含有两源共识且通过单源/最终质量门的行；未复核、冲突、缺失和不支持
   范围分别计数并可在 DataCenter 查询。

导入必须按源 manifest SHA 幂等：同内容重跑不复制、不产生新 Raw；源内容变化创建新导入
批次并阻断旧幂等键。不得使用硬链接让 V3 生命周期影响旧项目文件。

### K3D：生产 EOD、恢复与 DataCenter

- 在既有 K2 `ResolutionRun/ResolutionDecision` 上扩展固定四来源，不建设任意供应商矩阵、
  source assessment 或人工 `SourceOverride`。
- 预先计划 A/B 全部调用与 C/D 最大异常预算；预算不足必须在对应来源首次调用前失败。
- Raw、单源 QualityRun、DifferenceReport、Resolution、最终质量、manifest/`COMPLETE` 和原子
  发布顺序保持不变；失败候选收束，不留悬挂 `BUILDING/VALIDATED`。
- 输入指纹加入 BaoStock 包/单位策略、四源路由版本、比较规则版本和迁移 manifest SHA。
- DataCenter 展示实际来源、调用/导入状态、旧数据覆盖、未复核/隔离计数和每行选择来源；
  不返回绝对路径、socket/server 细节或完整 provider payload。
- 更新只读 release verifier，使其逐一复验所有实际调用/导入批次；不得要求未调用的 C/D
  产生伪批次。

离线实现已按上述合同接入默认 composition root，并保留 K2 artifact/frozen command 读取兼容。
合成端到端已覆盖 A/B 一致时 C/D 零调用、A 失败后 B/C 发布、A/B/C 无共识后 A/D 发布、
C 预算超限时首次腾讯调用前阻断、四源 DataCenter 安全投影，以及 immutable/read-only verifier
前后文件快照不变。`legacy_import_manifest_sha256=None` 也作为“本次不消费旧导入”的显式输入
指纹事实冻结；实际迁移时必须替换为已验收 manifest SHA。

## 授权边界

设计和离线合成开发不授权以下行为：

- 再次从包索引下载或改锁 BaoStock（本轮 0.9.3 精确落锁授权已使用）；
- 连接 BaoStock 或任何 AKShare upstream；
- 读取或修改 `E:\QuantKing`；
- 写入 `E:\QuantKingV3Data` 或访问 F 盘；
- 启动 XTQuant、QMT、旧项目、券商客户端或连接账户；
- 开始 S1-08R2 或 Stage 2。

每个真实步骤必须提供可复制的 dry-run、固定输入、调用/字节预算、零重试规则和失败后停止
条件。全量旧历史复核不能沿用小样本探针授权。

## 验收条件

- BaoStock 精确落锁，离线 package contract、适配器、单位和错误分类测试通过。
- 有界真实探针证明四来源在声明范围可读取，或把实际失败保留为阻断证据；不以合成结果替代。
- 新比较规则由代表性真实证据支持，边界用定点数验证，旧失败证据未修改。
- 旧根前后指纹一致；5,208 个候选文件的实际发现/读取/导入/隔离计数守恒，任何与 Stage 0
  元数据差异均有报告。
- 每个受管导入对象绑定源相对路径与双侧 SHA；导入幂等、可中断且失败不污染 Warehouse。
- 每个已发布历史行至少有旧 X 与一个独立服务源一致，或由至少两个独立服务源推翻并替换 X；
  无共识行不发布。
- 正常 EOD 的 A/B 一致路径为 C/D 零调用；各级失败、缺行、质量失败和冲突路径均有回归。
- DataCenter 与只读 verifier 可复验 BaoStock、三个 AKShare upstream、旧导入和最终版本血缘。
- 锁文件、Ruff、format、mypy、全部离线 pytest、前端、E2E、Markdown 和 `git diff --check`
  全部通过；真实 marker 结果单列，不混入离线通过数字。
- 独立主 agent 验收并形成提交后，才能开始 S1-08R2。

## 明确不在范围

- 迁移旧项目代码、数据库、配置、模型、日志或 UI；
- 恢复 XTQuant/QMT 生产适配或连接券商；
- 把旧数据直接复制进 Warehouse 或修改既有发布版本；
- Tushare、JQData 或其他新供应商；
- 分钟/Tick、复权研究版本、动态股票池、因子、模型或 Stage 2；
- 自动调度、无限重试、代理池、绕过网站限制或 IP 规避机制。
