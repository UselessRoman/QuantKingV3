# S1-07K1 实现交接

## 结论

S1-07K1-D1 的唯一东财直连复验已由主 agent 执行：target=`2026-06-11..2026-08-09`，upstream=`2026-05-27..2026-08-09`，两只证券各尝试一次、零重试，2/2 均得到 52 行 upstream、41 行 Canonical、`complete=True`。结合 R1 腾讯/Sina 四组 complete、冻结单位策略和全套离线合同，K1 功能验收现已通过。D1 后完整固化质量门 935 passed，全部后端、前端、E2E 和静态门禁全绿。D1 授权已消耗，严禁重跑；K1 已提交为 `f98f1d9`。K2 后续经用户明确授权并已独立主验收；S1-08 与 Stage 2 仍未获授权。

## 已实现

- `DataProvider` 固定为 `AKSHARE`，`DataUpstream` 固定为 `EASTMONEY`、`TENCENT`、`SINA`；registry 精确包含三个未复权日线 endpoint。
- AKShare 只在数据适配器显式调用时延迟导入；默认 import 和全部离线测试不访问网络。证券代码、日期和 endpoint 参数由集中模块转换。
- 三套响应进入 provider-neutral Raw，再映射到既有 `daily_bar_unadjusted.v1`。Canonical v1 保留 `pre_close`、`volume=share`、`amount=cny_yuan`；未验证单位不能进入 Canonical。
- 返回量额单位由 `akshare-1.18.83-returned-units.v1` 按 upstream 和证券代码解析；Raw/provider payload 数值原样保存，只有 Canonical normalization 将“手”乘 100 转为“股”。
- 若 endpoint 不直接提供 `pre_close`，只允许从同 upstream、按日期排序的前一日 close 派生后续行；首行缺失会产生显式 partial 结果，不用空表冒充成功。
- Raw/批次血缘区分 provider、upstream、endpoint、AKShare version、fetched_at、请求范围、row_count、内容 SHA 和 batch id。
- 正式 runtime/EOD gateway 已切换为 AKShare，production EOD 只接受 `daily_bar_unadjusted`；其他五类 schema 在任何 upstream 调用前稳定拒绝。
- active source assessment/readiness API、服务和 UI 已删除；Alembic 0010～0013 原样保留。`SourceOverride` 与 EOD evidence 仅作 K2 前过渡，没有引入 `ResolutionDecision` 或新状态。

## AKShare 1.18.83 离线源码审计

- `akshare.__version__` 为 `1.18.83`，`pyproject.toml` 与 `uv.lock` 均精确锁定该版本。
- 东财 `stock_zh_a_hist(symbol, period, start_date, end_date, adjust, timeout)`：目标调用参数与真实签名一致；函数内一次 `requests.get`，无显式 retry。输出包含日期、OHLC、成交量、成交额和涨跌额，但不直接提供 `pre_close`。官方单位说明为成交量“手”、成交额“元”。
- 腾讯 `stock_zh_a_hist_tx(symbol, start_date, end_date, adjust, timeout)`：目标调用参数与真实签名一致；先调用 `get_tx_start_year`，该 helper 首次 GET 为空时会再执行一次 fallback GET，随后按涉及年份逐年 GET；没有应用层显式 retry。固定 60 日窗口只跨 2026 年，但 start-year fallback 是否发生仍由上游响应决定。文档声称 volume 统一为股、amount 统一为元，但 1.18.83 源码只在 symbol 不以 `sh688/sz399/sh000/sz000` 开头时将 volume 乘 100；真实 `sz000001` 因此仍返回“手”。接口不提供 `pre_close`。
- 新浪 `stock_zh_a_daily(symbol, start_date, end_date, adjust)`：目标调用参数与真实签名一致；未复权路径固定执行历史行情和股本两个 GET，无 timeout 参数或显式 retry，源码警告大量抓取容易封 IP。输出 volume 为股、amount 为元，并主动删除 `prevclose`，因此不提供 Canonical `pre_close`。
- 生产参数现在为东财、腾讯显式传 `timeout=15.0`；锁定版本的新浪函数没有 `timeout` 形参，因此明确不传，避免建立虚假包合同或令已验证新浪路径产生 `TypeError`。
- 三个 endpoint 均不能直接保证 Canonical `pre_close`。固定窗口首行只能保持显式 partial，不使用东财“涨跌额”反推，也不伪造窗口前一交易日。

## 原固定真实探针证据

- 固定范围：`600000.SH`、`000001.SZ`，截至 2026-08-09 含首尾共 60 个自然日；每 upstream/证券一次 AKShare 函数调用，合计 6 次，零应用层重试。6/6 调用均成功，原授权已消耗，不得以原参数重跑。
- 每个 upstream/symbol 都得到 Raw 41 行、Canonical 40 行、`complete=False`；唯一缺口是请求窗口首行无同 upstream 前收盘，后 40 行均由同 upstream 前一行 close 安全形成 `pre_close`。
- 两只证券的 A/B 都有 40 个共同日期；OHLC 与派生 `pre_close` 价格最大差为 0，amount 比例约为 1。
- `600000.SH`：东财 volume 为手，腾讯和新浪为股；Raw 东财/腾讯比约 0.01。
- `000001.SZ`：东财和腾讯 Raw volume 都为手，新浪为股；Raw 东财/腾讯比约 1，新浪约为前两者的 100 倍。这证明腾讯文档中的全局“统一为股”不适用于锁定版本的 `sz000*` 分支。
- marker 的 8 个最终阻断为 6 个 `PARTIAL_RESULT` 和两只证券各 1 个 `AB_SUMMARY_UNAVAILABLE`。后两项不是新的调用/价格差异故障，而是聚合器因 A/B 均 `complete=False` 派生的同一首行边界；不得把 6/6 调用成功冒充 marker 通过。

## 已冻结量额策略

- 只对白名单沪深 A 股段解析单位：SH `600/601/603/605/688`，SZ `000/001/002/003/300/301`。
- 东财白名单证券返回 volume=手；新浪返回 volume=股；腾讯 `000xxx.SZ` 返回 volume=手，其余白名单证券返回 volume=股；三者 amount=元。
- SH 900、SZ 200、`sh000`/`sz399` 指数、SH 689 CDR、北交和未知代码段稳定拒绝；非 1.18.83 包版本保持 `UNIT_UNVERIFIED`，不得继承本策略。
- 生产 EastMoney gateway 已使用该冻结策略，不再被旧的全局 `UNIT_UNVERIFIED` 阻断；真实 endpoint 的首行 partial 仍保持发布阻断。

## S1-07K1-R1 固定 lookback 修复

- `DailyBarRequest` 继续表达目标窗口。集中参数层将 upstream start 固定为 `target_start - 15 natural days`，upstream end 与 target end 相同；60 个含首尾目标日期对应 75 个含首尾上游日期。
- 每个 upstream/证券仍只调用一次原 endpoint，不增加 endpoint、不自动 retry/fallback。AKShare 包内部 HTTP 行为仍按上方 1.18.83 源码审计披露。
- adapter 对 `upstream_start..target_end` 全部上游行排序并在同 upstream 内从前一行 close 派生 `pre_close`，然后裁掉 `target_start` 前的 context。Raw/provider payload 原样保留 context+target，`upstream_row_count` 统计全部上游行；Canonical 只含目标窗口，`canonical_row_count` 只统计裁回后的目标行。
- `complete` 只判断上游返回的目标行能否全部形成 `pre_close`。有目标行但无目标前 context seed 时仍为 `PARTIAL_RESPONSE`；目标窗口无行时 adapter 可为 complete，但 ingestion 仍要求显式 `NO_TRADING_SESSION`，否则为 `UNEXPECTED_EMPTY`。
- Raw response 顶层绑定 `target_start/end`、`upstream_start/end`、`lookback_days=15` 和 target chunk；manifest 同时记录这些范围、provider/upstream/endpoint/version、内容 SHA 与双 row count。重建会拒绝丢失、篡改或与 target chunk 不一致的 request-window binding。
- `real_akshare` 测试代码已增加 target/upstream 范围、lookback、双 row count 和 complete 输出，但开发 agent 未运行。新授权的 2 证券×3 upstream、每组合最多一次、总计最多六次真实调用只由主 agent 执行。

## R1 唯一真实探针证据

- target=`2026-06-11..2026-08-09`，upstream=`2026-05-27..2026-08-09`，lookback=15；`600000.SH`、`000001.SZ` × 三 upstream 共 6 个组合均已尝试，每组合最多一次 AKShare 函数调用，零应用层重试。本授权已消耗，不得重跑。
- 东财 `600000.SH` 与 `000001.SZ` 均稳定归类为 `AKSHARE_NETWORK`，没有 Raw/Canonical 结果。
- 腾讯两组与新浪两组均为 `upstream_rows=52`、`canonical_rows=41`、`complete=True`；Canonical 日期范围为 `2026-06-11..2026-08-07`。15 日 context 没有进入 Canonical，但成功为目标首个交易日提供同源 `pre_close`。
- 单位策略与冻结合同一致：腾讯 `600000.SH` volume=share、腾讯 `000001.SZ` volume=lot_100_shares、新浪两组 volume=share，四组 amount=cny_yuan。
- marker 的 4 个阻断为两条 `CALL_FAILED:EASTMONEY:*:AKSHARE_NETWORK` 和两条 `AB_SUMMARY_UNAVAILABLE`。后两项由 A=东财没有结果派生，不代表腾讯/Sina 的 lookback 或 Canonical 完整性失败。
- 结论只能写“R1 lookback 已被腾讯/Sina 真实验证；东财网络失败使三源/K1 功能验收仍不通过”，不能写成 K1 完成或真实 marker 通过。

## S1-07K1-R2 东财复核结果

- 独立入口为 `tests/compatibility/test_akshare_eastmoney_recheck.py`，不导入或调用会循环三 upstream 的原 marker。
- 独立环境开关为 `QUANTKING_RUN_REAL_AKSHARE_EASTMONEY_RECHECK=1`；默认缺少开关时在构造 adapter 前 skip，继续使用 strict `real_akshare` marker，并被默认完整质量门排除。
- 真实入口只构造一个 EastMoney adapter，固定依次请求 `600000.SH`、`000001.SZ`。target=`2026-06-11..2026-08-09`，upstream=`2026-05-27..2026-08-09`，lookback=15；每只最多一次函数调用，总计最多两次，无 retry/fallback。
- 两次尝试完成后才统一输出受控证据并聚合失败。证据限 endpoint/version、target/upstream 范围、lookback、双 row count、complete、Canonical 日期范围、字段类型、冻结单位策略和一个 numeric sample。
- 成功合同要求 AKShare/version=`1.18.83`、unit policy=`akshare-1.18.83-returned-units.v1`、upstream/canonical 均非空、`complete=True`、目标首日存在非 bool 数值 `pre_close`、Canonical 不含 context、volume=lot_100_shares、amount=cny_yuan。
- fake adapter 离线测试证明调用集合恰为两只证券、首只错误后仍尝试第二只、错误不会隐藏、未授权时 skip，以及默认质量门排除 marker。开发 agent 未运行 R2 真实 marker。
- 主 agent 唯一执行结果：两次授权调用均已尝试、零重试；两只证券均为 `AKSHARE_NETWORK`，没有 upstream rows、Raw 或 Canonical。`EASTMONEY_RECHECK_FAILURES count=2`，内容为两条对应证券的 `CALL_FAILED:*:AKSHARE_NETWORK`。
- pytest 结果为 `1 failed, 4 deselected`（1.36s）。失败代表真实上游阻断被正确暴露，不得改写成探针或 K1 通过。

## S1-07K1-D1 诊断与唯一直连复验结果

- 锁定的 AKShare 1.18.83 与当前 main 的东财函数都访问 `push2his.eastmoney.com`。执行 R1/R2 时进程的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 均指向 loopback，且 `NO_PROXY` 不含东财域名；官方 issues #6198、#6100、#6204、#7051 也记录过 `RemoteDisconnected`、封 IP 或 push2 不可用。
- `SourceAdapterError` 的网络错误新增稳定、脱敏的 `network_reason`：`proxy`、`remote_disconnected`、`dns`、`tls`、`timeout`、`connection`、`unknown`；非网络错误为 `None`。分类器循环安全遍历 cause/context 和已知的 exception-valued 属性，优先级固定为 proxy > remote_disconnected > dns > tls > timeout > connection > unknown；429 仍为 `AKSHARE_RATE_LIMITED`。
- 分类只读取异常类型名、429 status 和 exception-valued 链，不保留或输出第三方 message、URL、主机名、代理地址/凭据。合成测试覆盖 requests `ProxyError`、`SSLError`、`Timeout`，`ProtocolError -> RemoteDisconnected`、`gaierror`、普通连接及 unknown，并证明消息不泄漏。
- 独立 D1 入口为 `tests/compatibility/test_akshare_eastmoney_direct_recheck.py`，使用未消耗的新 guard `QUANTKING_RUN_REAL_AKSHARE_EASTMONEY_DIRECT_RECHECK=1` 和 strict `real_akshare` marker；不复用 R2 入口或 guard。
- 入口仍固定 target=`2026-06-11..2026-08-09`、upstream=`2026-05-27..2026-08-09`、lookback=15，只构造一个 EastMoney adapter，对 `600000.SH`、`000001.SZ` 各最多一次，总计最多两次，零应用层 retry/fallback。两次尝试后才统一输出稳定 error code/network reason 或受控成功证据。
- guard 在 adapter 构造前检查。仅真实 D1 测试的单线程上下文临时清除并在 `finally` 精确恢复 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及其三个小写键，保留 `NO_PROXY` 原值；生产 adapter 不修改全局代理策略，也未 monkeypatch AKShare/requests。
- 开发 agent 只运行离线合成合同，未设置 guard、未运行 D1 marker、未进行任何网络访问。主 agent 随后获得唯一 D1 授权并完成固定两次调用。
- 唯一真实结果：`600000.SH`、`000001.SZ` 均为 endpoint=`stock_zh_a_hist`、AKShare=`1.18.83`、upstream rows=52、Canonical rows=41、`complete=True`，Canonical 日期均为 `2026-06-11..2026-08-07`；volume=`lot_100_shares`、amount=`cny_yuan`、unit policy=`akshare-1.18.83-returned-units.v1`。
- marker 结果为 `1 passed, 4 deselected`（1.41s），`EASTMONEY_DIRECT_RECHECK_FAILURES count=0`；总计两次 endpoint 调用、零应用层重试，退出后六个代理键已精确恢复。
- 固定请求的代码、endpoint、日期、单位和调用次数均不变，清除当前代理路径后东财由 R1/R2 的两组 NETWORK 变为 D1 的 2/2 complete。该对照实证根因在当前代理路径/出口，而不在 adapter、lookback、schema 或单位归一化；不据此扩展为对其他网络环境的普遍结论。

## 删除清单

- XTQuant 生产实现：`sources/xtquant.py`、`sources/capabilities.py`、`xtquant_probe.py`，以及 XTQuant adapter/mapping/ingestion/real probe 专用测试。
- XTQuant 配置、环境变量、pytest marker 和质量脚本 marker。
- active `data/assessment` 包、`SourceCandidateAssessment` ORM/repository/UoW 方法、24 单元矩阵和专用测试。
- DataCenter source readiness API/服务/UI 和对应正向合同测试；保留一个 404 负向路由回归。

## EOD 回归保留范围

`tests/integration/data/test_eod_worker.py` 保留 62 个 provider-neutral 用例并全部通过，覆盖幂等、失败隔离、发布后故障、evidence/checkpoint、取消、base-version、manifest 深验、lease 与恢复。只删除两条与 K1 单日线 production 明确冲突的测试：六静态数据集整批执行，以及混合双数据集 reused/published 恢复。非日线 production 请求的“调用前拒绝”测试继续保留。

## 已执行验证

- R1 安装包合同、parameters/adapter/unit-policy/normalization/ingestion/real-probe-summary 聚焦：71 passed，1 deselected；开发 agent 未运行真实 marker。
- 主 agent 已执行 R1 唯一真实 marker：6/6 组合已尝试、零重试；4 个组合 complete，最终因 4 个上述阻断项失败。开发 agent 没有再次运行。
- R2 专用探针离线合同：4 passed、1 deselected。
- 主 agent 唯一 R2 marker：`1 failed, 4 deselected`（1.36s）；两次调用均为 `AKSHARE_NETWORK`。开发 agent 未运行或重试。
- D1 parameters/adapter/network-classifier/R2/D1 聚焦离线合同：35 passed、2 deselected；两项 deselected 分别是 R2 与 D1 真实 marker，均未运行。
- 主 agent 唯一 D1 marker：`1 passed, 4 deselected`（1.41s）；两只证券均 complete，failure count=0，代理环境已恢复。开发 agent 未运行或重试。
- `uv lock --check --offline`：Resolved 222 packages；`uv sync --frozen --all-groups --offline`：Checked 216 packages。
- EOD 聚焦：62 passed。
- 依赖落锁前的完整离线 pytest 基线：871 passed，2 skipped，3 deselected，0 failed（187.73s）。
- Ruff lint：通过；Ruff format：212 files already formatted；mypy：108 个源码文件通过。
- 前端 typecheck：通过；Vitest：4 files / 17 tests passed；生产 build：通过。
- `/root` 在 R1 实现前完整运行固化 `scripts/quality.ps1`：离线 pytest 909 passed、2 skipped、3 deselected；该结果只作 R1 前基线。
- `/root` 在 R1 后再次完整运行固化 `scripts/quality.ps1`：离线 pytest 918 passed、2 skipped、3 deselected；Ruff、format、mypy（110 个源码文件）、架构边界、Markdown 链接、前端 typecheck、17 个单元测试、生产 build、Playwright E2E 1 项和 `git diff --check` 全部通过。
- `/root` 在 R2 后完整运行固化 `scripts/quality.ps1`：离线 pytest 922 passed、2 skipped、4 deselected、4 warnings（184.41s）；Ruff lint、216 files formatted、mypy（110 个源码文件）、架构边界、Markdown 链接、前端 typecheck、17 个单元测试、生产 build、Playwright E2E 1 项和 git whitespace 全部通过；总脚本 218.2s。
- `/root` 在 D1 后完整运行固化 `scripts/quality.ps1`：离线 pytest 935 passed、2 skipped、5 deselected、4 warnings（185.28s）；Ruff lint、218 files formatted、mypy（110 个源码文件）、架构边界、Markdown 链接、前端 typecheck、17 个单元测试、生产 build、Playwright E2E 1 项和 git whitespace 全部通过；总脚本 222.3s。该结果是当前验收主证据，R2 的 922 passed 仅作历史基线。

## `rg` 剩余命中分类

- 历史证据与架构说明：Stage 0、S1-01～S1-06 验收文档和旧路线说明保留 XTQuant/QMT/BaoStock/Tushare 文字，只作审计输入。
- 历史数据库字节：Alembic 0010～0013 与迁移升级测试保留 `SourceCandidateAssessment`、`BLOCKED_EXTERNAL_VALIDATION` 和 `XTQUANT` 字面量，以验证旧数据库可升级。
- 安全/盘点基础：`legacy_inventory.py` 的关键字分类和 backup 的“零 QMT”副作用声明不是生产行情路径。
- 负向合同：DataCenter `/api/data/sources` 404 测试和架构边界中的违规 `akshare` import 样例用于证明已退役/禁止路径。
- active runtime、配置、API、Web 和正常 fixture 不再含 XTQuant/QMT、旧 assessment/readiness 或 BaoStock/Tushare 生产语义。

## 独立验收阻断与下一步

1. 原三源、R1、R2 与 D1 的真实授权均已消耗；禁止重试、重跑 marker、扩大范围或访问其他 upstream。
2. D1 后完整固化质量门已通过；935 passed 等当前精确证据见上方，R2 的 922 passed 仅作历史基线。
3. R1 腾讯/Sina 四组与 D1 东财两组均 complete；K1 功能验收通过。
4. K1 已提交为 `f98f1d9`；K2 后续经用户明确授权并已独立主验收。S1-08 仍须另行授权。
