# S1-07A 离线验证框架交接

状态：离线验证框架已提交、等待主验收。真实 XTQuant、BaoStock、AKShare、Tushare 和
`E:\QuantKing` 旧数据均未访问、未获授权；S1-07 未完成，S1-08 未开始。

## 范围与基线

- Stage：Stage 1 / S1-07A。
- 基线：`44d268b3db8bd9c690225f62e60fa12bf56a289d`。
- 只实现 provider-neutral 合同、受管理证据 writer/verifier、离线 CLI 和测试门。
- 没有迁移数据、发布 `DatasetVersion`、创建 `SourceOverride`、启用 auto-switch 或开始
  S1-08/Stage 2。

## 首次红测

命令：

```powershell
uv run --offline --frozen pytest tests/data/validation -q -p no:cacheprovider
```

首次结果为 `14 failed`，全部因 `quantking.data.validation` 尚不存在，证明测试先于实现。

## 固定样本 v1

- 版本：`s1-07.fixed-sample.v1`
- SHA-256：`7d1007817928189e39d038cc8afce00d35d26c89877f217f65cf7e9894b6e2b3`
- 日期范围全部显式为 `2024-01-02`～`2024-01-05`，没有 `today/latest`。

| case_id | dataset | code/exchange | purpose |
|---|---|---|---|
| `adjustment_normal_sh` | adjustment_factor | `600000.SH` | NORMAL |
| `benchmark_sh_composite` | benchmark_index_daily | `000001.SH` | BENCHMARK |
| `benchmark_sz_component` | benchmark_index_daily | `399001.SZ` | BENCHMARK |
| `calendar_bj` | exchange_calendar | BJ | NORMAL |
| `calendar_sh` | exchange_calendar | SH | NORMAL |
| `calendar_sz` | exchange_calendar | SZ | NORMAL |
| `normal_bj_equity` | daily_bar_unadjusted | `430047.BJ` | NORMAL |
| `normal_sh_equity` | daily_bar_unadjusted | `600000.SH` | NORMAL |
| `normal_sz_equity` | daily_bar_unadjusted | `000001.SZ` | NORMAL |
| `security_master_normal_sz` | security_master | `000001.SZ` | NORMAL |
| `trading_status_normal_sh` | trading_status | `600000.SH` | NORMAL |

每个 case 适用于 XTQUANT、BAOSTOCK、AKSHARE、TUSHARE，形成 4 × 6 的待验证矩阵。来源不支持
某单元时必须以真实 `CAPABILITY_UNSUPPORTED` 证据保持阻断。事件目的枚举覆盖 IPO、LISTING、
DELISTING、ST、SUSPENSION、CORPORATE_ACTION；由于本次没有外部访问授权，尚未取得并复核
权威事件依据，因此没有猜测事件代码或日期并写入正式 v1。

## 合同与安全门

- REAL run 绑定 source × dataset、请求 SHA、样本 SHA、adapter/package/code/schema/rule/
  threshold/config 版本、Raw manifest、Canonical 内容、QualityRun、可选 DifferenceReport、
  五项 gate、时间范围和稳定脱敏失败分类。
- `PRODUCTION_READY` 需要 REAL、五项 gate 全过和至少三个不同 run ID。
- writer 使用 `.incomplete` 构建、规范自签名 manifest、逐文件 SHA/大小/行数、`COMPLETE`
  和原子 rename；相同 ID/相同字节为 no-op，不同内容冲突。
- verifier 拒绝 manifest、COMPLETE、文件集合、文件 SHA、路径和 lineage 篡改，以及
  symlink/junction/reparse point 和路径逃逸。
- SQLite assessment 保存入口只接受 `VerifiedAssessmentEvidence`；合成 fixture helper
  明确拒绝 REAL。
- `plan`、`--help`、import 和 `status` 不导入供应商 SDK。`execute` 需要精确 acknowledgement，
  provider runner 只在该门之后延迟加载。

## 离线 CLI

```powershell
python scripts/run_source_validation.py plan --source XTQUANT --dataset exchange_calendar --output-root <authorized-absolute-root>
python scripts/run_source_validation.py execute --source XTQUANT --dataset exchange_calendar --output-root <authorized-absolute-root> --ack I_ACKNOWLEDGE_AUTHORIZED_REAL_SOURCE_VALIDATION
python scripts/run_source_validation.py verify --validation-root <authorized-absolute-root> --directory <evidence-directory>
python scripts/run_source_validation.py status --validation-root <authorized-absolute-root>
```

`plan` 只展示固定 case、范围、字段、SDK、未来 execute 的网络/QMT访问、输出目录、副作用和完整
无密命令。S1-07A 不包含任何 provider runner，故即使 acknowledgement 正确也不能伪造真实成功。

## 外部访问与副作用

- 网络：无。
- QMT/XTQuant：未连接、未加载。
- BaoStock/AKShare/Tushare：未安装、未加载、未调用。
- 正式根 `E:\QuantKingV3Data`：未访问。
- 旧目录 `E:\QuantKing`：未访问。
- F 盘备份：未访问。
- 仓库内副作用：源码、测试、文档和 marker/离线质量门更新；没有原始真实数据或凭据。

## 当前 source × dataset 结论

| source | datasets | 五项 gate | 稳定运行数 | 结论 |
|---|---|---|---:|---|
| XTQUANT | 六类 | 均未真实评估 | 0 | BLOCKED_EXTERNAL_VALIDATION |
| BAOSTOCK | 六类 | 均未真实评估 | 0 | BLOCKED_EXTERNAL_VALIDATION |
| AKSHARE | 六类 | 均未真实评估 | 0 | BLOCKED_EXTERNAL_VALIDATION |
| TUSHARE | 六类 | 均未真实评估 | 0 | BLOCKED_EXTERNAL_VALIDATION |

没有生成 DifferenceReport。没有读取 Stage 0 的三组旧数据候选，因此它们仍是待授权审计，
没有 `DATA_ONLY` 或 `REJECTED` 结论。

## SDK 与质量结果

- XTQuant/BaoStock/AKShare/Tushare SDK：均未为 S1-07A 加载或新增依赖；没有可报告的本次
  真实执行 SDK 版本。任何 S1-07B/C plan 必须先显示并冻结精确版本。
- `scripts/quality.ps1`：通过。
- 离线 pytest：`987 passed, 2 skipped, 3 deselected`。
- 前端单测：`17 passed`；生产构建通过；Playwright：`1 passed`。
- Ruff、mypy、锁文件、架构边界、Markdown 链接和 `git diff --check` 全部通过。
