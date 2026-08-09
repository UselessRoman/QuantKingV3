# Stage 1 来源注册表

状态：R1 腾讯/Sina 四组与 D1 东财两组均已验证 complete；K1 功能验收通过。原三源、R1、R2 和 D1 的真实授权均已消耗，禁止重跑。

本文只是三个固定 upstream 的静态注册表，不是 source×dataset 能力矩阵，不包含 `PRIMARY/FALLBACK` 审批、`PRODUCTION_READY` 或稳定运行次数。

| source_id | provider | upstream | endpoint | 角色 | 当前状态 |
|---|---|---|---|---|---|
| `AKSHARE_EASTMONEY` | AKSHARE | EASTMONEY | `stock_zh_a_hist` | A：东财主来源 | D1 两组 52→41；complete；volume=手 |
| `AKSHARE_TENCENT` | AKSHARE | TENCENT | `stock_zh_a_hist_tx` | B：腾讯常态互验 | R1 两组 52→41；complete；volume 按 symbol |
| `AKSHARE_SINA` | AKSHARE | SINA | `stock_zh_a_daily` | C：新浪异常第三源 | R1 两组 52→41；complete；volume=股 |

冻结策略固定到 AKShare 1.18.83 和 K1 白名单代码段：东财成交量为手，新浪为股；腾讯 `000xxx.SZ` 为手，其余白名单沪深 A 股为股；三者成交额均为元。腾讯官方“统一为股”的描述与 `sz000*` 锁定源码/真实结果不符，因此不得把腾讯建模为单一 volume unit。Raw 数值不改，Canonical normalization 才把手乘 100。

## 固定规则

- 三者是同一个 Python 依赖中的三个上游，不得只记录 `AKSHARE` 而隐藏 upstream。
- A/B 正常时不调用 C；A/B 异常时 C 才进入仲裁。
- 最少两源一致才可产生发布候选。
- 选择优先级 `EASTMONEY > TENCENT > SINA` 只在一致集合内部生效。
- 不再评估 XTQuant、BaoStock、Tushare 或 JQData 作为 Stage 1 生产源。
- 本表已由固定 source registry 实现；不得按 `DatasetKind` 扩展成笛卡尔积。
