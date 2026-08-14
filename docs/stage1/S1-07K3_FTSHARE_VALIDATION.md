# S1-07K3 FTShare B 源候选验证

状态：B 源资格验收在 FTShare batch 证券标识合同处失败并停止；治理证据也不足。FTShare
当前判定为**不合格 B 源**，未进入生产配置，未改变既有 K3 路由或比较阈值。

## 目的

评估固定路线 `A=BAOSTOCK / B=FTSHARE / C=AKSHARE(TENCENT→SINA)` 中 FTShare 是否能作为
独立常态 B 源。本任务只验证候选，不授权将一次成功冒充生产验收。

## 已确认的离线事实

- 官方 SDK `pyproject.toml` 报告版本 `0.1.1`、Python 3.12 支持和 Alpha 状态。
- Python 包索引中没有可解析的 `ftshare==0.1.1`；当前只能从官方 GitHub 源安装。
- 本次固定官方 commit `8ffb97aef871b5ab787831652668dbb7f2001fed`，禁止跟随 `main`。
- 固定 commit 可在项目 Python 3.12 工具链构建和导入，默认网关为
  `https://market.ft.tech/gateway/`；项目锁文件未修改。
- 官方注册表提供 `stock_candlesticks` 和 `stock_candlesticks_batch`，但尚未证明返回字段、
  单位、底层上游、许可、限流或服务承诺满足 QuantKing 合同。

## 固定首轮真实边界

- 证券：`000001.SZ`、`600000.SH`。
- target：`2026-06-11..2026-08-09`；lookback：15 个自然日；upstream：
  `2026-05-27..2026-08-09`。
- FTShare：一次两证券批量 K 线和每证券一次单 K 线，共最多 3 次 API 调用；应用层零重试。
- 只有 FTShare 响应合同可解释后，才允许在同一任务上界内各调用 BaoStock、腾讯 2 次做比较。
- 不写 `E:\QuantKingV3Data`，不读取 `E:\QuantKing`，不访问 F 盘，不启动 QMT/券商，
  不激活阈值或生产路由。

## 阻断条件

以下任一项不成立，FTShare 不得成为 B 源：

1. 能说明 `stock_candlesticks` 的底层数据来源，并证明与 BaoStock、腾讯相互独立；
2. 明确未复权语义、时区、日期边界、价格精度、`pre_close`、成交量与成交额单位；
3. 单证券与批量接口在相同输入下逐行一致，且无缺行、重复键或静默修值；
4. 固定发行物可由 `uv` 精确锁定；SDK 许可证不能代替行情数据使用许可；
5. 有明确的调用限制、错误分类和足以支撑单机日频使用的可用性证据。

## 首轮真实结果（2026-08-14）

固定 marker `S1-07K3-FTSHARE-CANDIDATE-PROBE-V1` 已执行且授权耗尽：批量接口一次、
`000001.SZ` 单证券一次、`600000.SH` 单证券一次，共 3/3 次 API 调用，应用层零重试。
三次均返回 `FtshareHTTPError`，没有形成任何可解释的行情响应；因此 BaoStock 和腾讯比较
调用均为 0，正式数据根写入为 0。

首版探针只保留了稳定异常类型，未保留 HTTP 状态码，当前不能把失败进一步归因为鉴权、
参数校验、路由或服务端故障。探针已离线改进为未来只额外记录安全的整数状态码，但不得
借此自动重跑；任何新的真实调用必须使用新 marker 和新授权。

证据：[`evidence/k3_ftshare_candidate_probe_2026-08-14.json`](evidence/k3_ftshare_candidate_probe_2026-08-14.json)。

## R2 诊断结果（2026-08-14）

固定 marker `S1-07K3-FTSHARE-CANDIDATE-DIAGNOSTIC-R2` 已执行。R2 只允许先对
`000001.SZ` 发起一次单证券请求；只有成功才可发起一次同证券 batch 请求。首个请求到达
FTShare 网关后返回 HTTP `400`，无结构化 API code。任务因此在 1/2 次上界处停止，batch
未调用，应用层零重试，BaoStock/腾讯比较调用为 0，正式数据根写入为 0。

HTTP 400 排除了“FTShare 网关不可达”这一解释，但不能证明服务可用于生产。离线复核固定
SDK 源码发现：`adjust_kind` 是可选参数，Python 默认值为 `None`；SDK 会丢弃值为 Python
`None` 的请求字段。R2 实际发送的是字面字符串 `"None"`。官方文字把未复权写作
`None（默认）`，官方测试样例只覆盖 `Forward`，没有证明字符串 `"None"` 是合法枚举值。
因此，**最可能但尚未被真实调用证实**的根因是探针把“省略可选参数”误解成发送字符串
`"None"`。时间戳、证券代码或服务端其他校验仍不能完全排除。

R2 后的结论保持为“不接纳”：FTShare 尚未返回任何行情行，独立性、字段、单位、批量一致性、
许可和稳定性条件均未验证。若继续，最小 R3 应只允许一次 `000001.SZ` 单证券调用，保持其余
参数完全相同，仅把 `adjust_kind` 改为 Python `None`（从 JSON 中省略）；成功后才另行设计
数据合同验证。R2 授权已经消耗并结束，R3 需要新 marker 和新授权。

证据：[`evidence/k3_ftshare_candidate_r2_2026-08-14.json`](evidence/k3_ftshare_candidate_r2_2026-08-14.json)。

## R3 单变量修复结果（2026-08-14）

固定 marker `S1-07K3-FTSHARE-CANDIDATE-OMIT-ADJUST-R3` 已按最小授权执行。范围仍为
`000001.SZ`、target `2026-06-11..2026-08-09`、15 日 lookback、upstream
`2026-05-27..2026-08-09`；相对 R2 唯一参数变化是把 `adjust_kind` 从字符串 `"None"`
改为 Python `None`，由固定 SDK 从 JSON 中省略。共执行 1/1 次单证券 API 调用，应用层零
重试，未调用 batch、BaoStock 或 AKShare，未写正式数据根。

请求成功返回 52 行。顶层字段为 `code/data/message`；行情行字段为 `open/high/low/close`、
`volume/turnover`、`ts_millis/ts_millis_open`。首行收盘时间戳是
`2026-05-27 15:00:00+08:00`，开盘时间戳是 `2026-05-27 09:30:00+08:00`，与冻结
upstream 起点一致。响应 canonical JSON SHA-256 为
`591bc5e1e18541bbbc37de55582d6c54edc52482ccc8df8cb8856d41da1f41ae`。

R2 与 R3 构成一次受控单变量对照：相同接口、证券、范围及其他参数下，字符串 `"None"`
返回 HTTP 400，省略字段则成功。因此“未复权默认值必须省略而不是发送字符串”具有高置信度；
这解决的是请求契约，不等于 FTShare 已达到生产数据源标准。

R3 后 FTShare 仍保持候选状态，原因是尚未验证：

1. 52 行的末端覆盖、主键唯一性、逐日 OHLC 规则、缺失值和排序；
2. `volume` 是股还是手、`turnover` 的币种和单位、价格精度及 `pre_close` 构造；
3. 单证券与 batch 的逐行一致性；
4. 与 BaoStock、腾讯的逐日差异及底层数据来源独立性；
5. 行情数据使用许可、调用限制、版本发布和持续可用性。

证据：[`evidence/k3_ftshare_candidate_r3_2026-08-14.json`](evidence/k3_ftshare_candidate_r3_2026-08-14.json)。

## B 源资格验收结果（2026-08-14）

固定 marker `S1-07K3-FTSHARE-B-SOURCE-QUALIFICATION-V1` 已执行。冻结上界为 FTShare
两个 single 加一个双证券 batch，三者逐行一致后才允许 BaoStock、腾讯各两次；应用层零重试，
任一阶段失败即停止。

FTShare 两个 single 与一个 batch 共 3 次调用均完成到响应解析阶段，但 batch 返回的证券标识
集合不等于请求的 `000001.SZ / 600000.SH`。严格合同因此拒绝并停止，BaoStock、腾讯调用均为
0，总调用 3/7，正式数据根写入为 0。本版失败证据没有保留 batch 返回的安全证券标识列表，
因此不能离线判断它是 `XSHG/XSHE` 长后缀、其他规范化形式，还是缺少某个证券；不得猜测或
把这次失败写成行情值不一致。

即使后续解决 batch 标识映射，当前治理证据也不能满足独立 B 源门槛：

1. 官方仓库说明 SDK 连接“FTShare 数据服务”，但没有披露股票 K 线的底层供应商，无法证明
   它独立于 BaoStock 和 AKShare 腾讯/新浪；
2. GitHub 的 MIT 许可证只覆盖 SDK 源码，未发现行情数据使用许可；
3. 官方股票 K 线 API 文档描述请求参数，但未给出返回字段单位、修订政策、调用配额或 SLA；
4. 固定 `0.1.1` 在 Python 包索引中不可解析，只能固定 Git commit；官方 GitHub Releases
   页面没有可用发行物，版本供应链仍不满足生产锁定要求。

因此本次资格结论是“不接纳”。严重性为 High，置信度为高：技术上已经证明单证券接口可用，
但 batch 合同未通过，同时最关键的独立性和行情许可无官方证据。除非 FTShare 官方补充底层
来源独立性、数据许可、字段单位/修订/限流/SLA，并通过新的可诊断 batch 与跨源逐行验收，
不得将其配置为常态 B 源。

官方资料复核入口：[SDK 仓库](https://github.com/FTShare-Lab/FTShare-python-sdk)、
[股票 K 线 API Reference](https://github.com/FTShare-Lab/FTShare-python-sdk/blob/main/docs/API_REFERENCE.md)、
[SDK MIT License](https://github.com/FTShare-Lab/FTShare-python-sdk/blob/main/LICENSE)、
[GitHub Releases](https://github.com/FTShare-Lab/FTShare-python-sdk/releases)。

证据：[`evidence/k3_ftshare_qualification_attempt_2026-08-14.json`](evidence/k3_ftshare_qualification_attempt_2026-08-14.json)。
