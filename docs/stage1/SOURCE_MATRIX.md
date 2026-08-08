# Stage 1 数据源矩阵

状态：目标已确认，统一接入库为 AKShare，代码尚未完成 K1 替换。

| source_id | provider | upstream | endpoint | 角色 | 当前状态 |
|---|---|---|---|---|---|
| `AKSHARE_EASTMONEY` | AKSHARE | EASTMONEY | `stock_zh_a_hist` | A：东财主来源 | 待 K1 实测 |
| `AKSHARE_TENCENT` | AKSHARE | TENCENT | `stock_zh_a_hist_tx` | B：腾讯常态互验 | 待 K1 实测 |
| `AKSHARE_SINA` | AKSHARE | SINA | `stock_zh_a_daily` | C：新浪异常第三源 | 待 K1 实测 |

## 固定规则

- 三者是同一个 Python 依赖中的三个上游，不得只记录 `AKSHARE` 而隐藏 upstream。
- A/B 正常时不调用 C；A/B 异常时 C 才进入仲裁。
- 最少两源一致才可产生发布候选。
- 选择优先级 `EASTMONEY > TENCENT > SINA` 只在一致集合内部生效。
- 不再评估 XTQuant、BaoStock、Tushare 或 JQData 作为 Stage 1 生产源。
