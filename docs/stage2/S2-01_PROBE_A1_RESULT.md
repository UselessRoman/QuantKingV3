# S2-01 来源发现 a1 实际结果

2026-09-09，用户明确批准 [首轮计划](S2-01_PROBE_PLAN.md) 后执行一次。
结论：**请求完成，观察数据已保存；来源资格仍未通过，S2-01 / S2-02 不据此解锁。**

## 实际执行与完整性

```powershell
. .\scripts\activate-toolchain.ps1 -UvCacheDirectory .uv-cache
.\scripts\uv.ps1 run --offline --frozen python -m quantking.data.adapters.research_probe --execute-approved-a1
```

- 命令退出码 0；[result.json](evidence/s2-01-source-probe-a1/result.json) 为
  `OBSERVATIONS_COLLECTED / QUALIFICATION_STILL_REQUIRED / qualified=false`。
- 4 个固定证券，16 次业务调用 + login/logout = **18 次 SDK 调用全部返回**；
  1 次连接、18 次协议请求开始记录，重试/分页/重连 0。日志在
  [calls.jsonl](evidence/s2-01-source-probe-a1/calls.jsonl)。
- 实际耗时 **7.187 秒**；JSON 响应合计 **75,235 字节**，线缆接收累计 **24,161 字节**。
  每响应不超过 500 行，服务端页容量均为 2000；未触发失败停止条件。
- [plan.json](evidence/s2-01-source-probe-a1/plan.json) 绑定实际参数、预算、执行器和依赖锁 SHA。
  [sha256.json](evidence/s2-01-source-probe-a1/sha256.json) 的 21 个文件条目已逐项复算 SHA 和大小一致；
  清单自身 SHA 为 `dc598068fd04554f31c2d380534e2616beeeb8bd4e0cc0ae728b765f1dabf7ed`。
- 证据目录不可覆盖；没有写运行数据根，没有发布任何 DatasetVersion 或研究版本。
  后续解释写本文，不补改原响应和原清单。

## 观察事实与边界

| 证券 | 基础资料 | 日线观察 | 复权事件 / 分红行 |
|---|---|---|---|
| sh.600000 | ipoDate=1999-11-10，outDate 空 | 2023-01-03..12-29，242 行，isST=0 | 各 1 行，2023-07-21 现金分红 |
| sz.000001 | ipoDate=1991-04-03，outDate 空 | 2023-01-03..12-29，242 行，isST=0 | 各 1 行，2023-06-14 现金分红 |
| sz.002309 | ipoDate=2009-11-27，outDate 空 | 2024-01-02..06-28，117 行，isST 始终 1 | 两接口均零行，仅记录为空，不能证明无事件 |
| sh.688469 | ipoDate=2023-05-10，outDate 空 | 2023-06-01..07-31，41 行，isST=0 | 两接口均零行，仅记录为空 |

以上为本轮单一供应商观察，不是权威公告认证。日线合计 642 行；基础资料 4 行、复权 2 行、
分红 2 行，业务接口总计 650 行。没有将这些观察加入 Stage 1 多源日线发布。

关键发现：

1. [002309 日线](evidence/s2-01-source-probe-a1/response-11.json) 中 2024-04-24
   `tradestatus=0`，OHLC 和 preclose 均为 1.4500，**volume/amount 为空字符串**。
   已命中供应商停牌标记样本，但未取得独立公告证明；不能把空成交额自动填成 0。
2. 002309 在整段窗口 isST 都为 1，没有命中状态变化；该二元字段也无法区分 ST、*ST、退市整理。
   [基础资料](evidence/s2-01-source-probe-a1/response-10.json) 当前名称为“中利集团”、status=1，
   再次说明当前名称/当前状态不能用于历史风险状态回填。
3. [688469 基础资料](evidence/s2-01-source-probe-a1/response-14.json) 返回上市日 2023-05-10，
   早于批准窗口 2023-06-01，故**没有命中上市边界**。四个证券 outDate 都空，亦未命中退市边界。
   执行中没有因此扩大证券或日期。
4. [浦发分红](evidence/s2-01-source-probe-a1/response-05.json)：登记日 2023-07-20，
   除权/支付日 07-21，税前每股现金字段 0.32；税后为 **`0.288或0.32`**。
   [平安分红](evidence/s2-01-source-probe-a1/response-09.json)：登记日 2023-06-13，
   除权/支付日 06-14，税前 0.285，税后为 **`0.2565或0.285`**。
   这些税后值是非单值文本，不能直接转成 Decimal 或任选其一；持有期/税费规则尚需独立事实。
5. 两个分红样本的 `dividStocksPs=0.000000`，`dividStockMarketDate` 和送转公积字段为空；
   **未取得实际送转正例或新增股可卖日**，累计因子不能补齐这些事实。
6. [浦发因子](evidence/s2-01-source-probe-a1/response-04.json) 为
   foreAdjustFactor=0.893976、backAdjustFactor=adjustFactor=11.949786；
   [平安因子](evidence/s2-01-source-probe-a1/response-08.json) 为
   0.839609、104.876880、104.876880。每证券只返回一个窗口内事件点，没有窗口锚点之前
   的累积因子基值，故不能仅凭本轮恢复已冻结的 forward multiplier。
7. 参考字段实际返回了公告/登记/除权/支付**日期**，没有精确披露时间戳或修订版本。
   当前采集时点不能充当历史 available_at；尚未验证未来修订是否重算过去的累计值。

## 来源资格结论

本轮确认 SDK 端点可响应、字段形状与所选窗口的有限观察数据；四证券同 endpoint 的字段一致。
缺失项仍包括：权威或独立交叉证据、ST 类型及变化、上市/退市边界、真实送转、因子锚点、
字段单位的权威依据、可用时间与历史修订行为、除权交易参考价和税费执行口径。
完整历史证券集合、规范 SHA 和正式日线父版本扩展未进行。

因此三类参考数据仍未获生产资格，不能声明可正式研究或回测。已有停牌和现金分红观察可用于
下一轮定向取证选样，但当前授权已完成，不自动抓取公告、扩大窗口或进行第二次探针。

## 实现与离线复核

新增执行器与测试仅服务本次有界来源发现，不增加生产发布/来源状态机。
独立只读 SDK/执行器审查发现服务端页容量丢失、连接异常被 SDK 吞掉、解压尾部漏拒及活动调用
终止日志不足，主代理已修复并在离线测试覆盖。审核代理后续遇额度限制，修复后的验证属于
主代理自动测试，未标为独立复验。

定向 `pytest tests/data/adapters/test_research_probe.py -q --tb=short`：**14 passed**，
包括原生 `ResultData`、假 socket、父进程 watchdog、预算、禁止覆盖、首错无 logout 和 SHA 复算。
Ruff 和该模块 mypy 通过。2026-09-09 执行完整
`scripts/quality.ps1 -UvCacheDirectory .uv-cache`，退出码 0：
Python **1491 passed / 6 skipped / 3 deselected**（222.23 秒），前端 **17 passed**，
Web E2E **1 passed**（7.8 秒）；锁文件、冻结依赖同步、Ruff（299 文件）、mypy（139 源文件）、
架构边界、Markdown 链接、前端类型检查/生产构建及 Git whitespace 检查均通过。
E2E 是既有数据中心回归，不代表 Stage 2 研究页面已交付。
