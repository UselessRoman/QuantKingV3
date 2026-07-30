# S1-07B 离线准备交接

状态：S1-07B-R1 离线准备修复已提交，权威样本资格仍阻断，等待外部只读授权及人工复核。

## 范围与基线

- Stage：Stage 1 / S1-07B。
- S1-07B 基线：`1b818018e9b8ca9f56353b0e78b37008912ba86d`。
- S1-07B-R1 基线：`310b2758cdb4595fc3e89891c7ca0c30a927447e`。
- 本任务只实现权威固定样本 v3 的冻结/资格合同、引用证据 bundle/verifier，以及未来
  S1-07C 使用的 REAL runtime/signer readiness。
- 没有联网、QMT/MiniQMT、真实 XTQuant 或备用源调用；没有读取正式数据根、旧项目或备份根。
- 没有开始 S1-07C/D、S1-08 或 Stage 2。

## 首次红测

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/data/validation/test_s1_07b_samples_and_references.py tests/data/validation/test_s1_07b_runtime.py -q -p no:cacheprovider --tb=no
```

首次结果为 `15 failed, 1 passed`。根因是基线只有 v1 + v2 release、S1-07A synthetic-only
证据链和硬阻断 CLI，尚不存在 v3 冻结链、document/fact/case 引用 bundle verifier、固定
runner registry、只读 package metadata probe 或 version-scoped REAL runtime plan。

R1 红测命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/data/validation/test_s1_07b_r1_runtime.py -q
```

R1 首次结果为 `22 failed, 3 passed`。根因是 execute 在样本资格检查后直接进入 authorization
校验，没有在 claim 前机器复验 package version、source×dataset case scope、完整冻结版本和
安全 output root；plan 对 output root 的预先 resolve 还会丢失部分路径攻击信息。

## 固定样本版本链

| version | sample SHA-256 | release/规范字节 SHA-256 | 状态 |
|---|---|---|---|
| s1-07.fixed-sample.v1 | `bc40271a632e0f8b4bae493806bf3d316897857a1102ed01b70f2bb46c632bee` | `aafb03db4a3845aa710848dc1f12542350afbb57d6edae466cf0978b5c0bc249` | 冻结、不改写 |
| s1-07.fixed-sample.v2 | `03d3d6c1e898b0746b7c5ab3911917a600fa3975f802c1d414fd0b6196c80dfb` | `2b692c631018c1e600680af329d3936dd98267d9d82ffdbcbe854b3ca8574cb8` | 冻结、不改写 |
| s1-07.fixed-sample.v3 | `5b5bf202e17181dfcbd9159f261dc9b4c636fb7641a3786523b42f952c6f3200` | `52bc879ed5f42ea6a8fe0d6b489d4d9c1153e3568379d614539b65ca207e72eb` | `FRAMEWORK_ONLY` |

v3 直接绑定 v2 sample SHA。对 v3 release 的 eligibility、缺口、previous SHA、用途集合、版本
或 sample SHA 作任何修改都会偏离仓库受控规范字节并被拒绝。v3 的
`PRODUCTION_ELIGIBLE` 只能由完整 reference bundle、受控 reviewer trust 和完整性 verifier
共同推导；当前 production reviewer key allowlist 为空。

## 当前 v3 框架 case

以下 11 个 case 原样继承 v2，只证明请求/schema/unit 框架，不证明证券在所列日期的事件事实。
其现有 locator 是仓库 Canonical 合同，不是一手事件材料，不能使 v3 获得生产资格。

| case_id | dataset | code/exchange | date range | purpose | expected facts | authority | locator |
|---|---|---|---|---|---|---|---|
| adjustment_normal_sh | adjustment_factor | 600000.SH / SH | 2024-01-02..2024-01-05 | NORMAL | price/volume multiplier、基准日、算法版本合同 | 未取得事件权威材料 | `docs/stage1/DATA_CONTRACTS.md` |
| benchmark_sh_composite | benchmark_index_daily | 000001.SH / SH | 2024-01-02..2024-01-05 | BENCHMARK | close/pre_close/volume/amount 单位合同 | 未取得指数日值权威材料 | `docs/stage1/DATA_CONTRACTS.md` |
| benchmark_sz_component | benchmark_index_daily | 399001.SZ / SZ | 2024-01-02..2024-01-05 | BENCHMARK | close/pre_close/volume/amount 单位合同 | 未取得指数日值权威材料 | `docs/stage1/DATA_CONTRACTS.md` |
| calendar_bj | exchange_calendar | BJ | 2024-01-02..2024-01-05 | NORMAL | 每个自然日及开闭市合同 | 未取得交易所日历材料 | `docs/stage1/DATA_CONTRACTS.md` |
| calendar_sh | exchange_calendar | SH | 2024-01-02..2024-01-05 | NORMAL | 每个自然日及开闭市合同 | 未取得交易所日历材料 | `docs/stage1/DATA_CONTRACTS.md` |
| calendar_sz | exchange_calendar | SZ | 2024-01-02..2024-01-05 | NORMAL | 每个自然日及开闭市合同 | 未取得交易所日历材料 | `docs/stage1/DATA_CONTRACTS.md` |
| normal_bj_equity | daily_bar_unadjusted | 430047.BJ / BJ | 2024-01-02..2024-01-05 | NORMAL | close/pre_close/volume/amount 单位合同 | 未取得证券日值权威材料 | `docs/DATA_ARCHITECTURE.md` |
| normal_sh_equity | daily_bar_unadjusted | 600000.SH / SH | 2024-01-02..2024-01-05 | NORMAL | close/pre_close/volume/amount 单位合同 | 未取得证券日值权威材料 | `docs/DATA_ARCHITECTURE.md` |
| normal_sz_equity | daily_bar_unadjusted | 000001.SZ / SZ | 2024-01-02..2024-01-05 | NORMAL | close/pre_close/volume/amount 单位合同 | 未取得证券日值权威材料 | `docs/DATA_ARCHITECTURE.md` |
| security_master_normal_sz | security_master | 000001.SZ / SZ | 2024-01-02..2024-01-05 | NORMAL | listing/effective interval/status 合同 | 未取得 point-in-time 权威材料 | `docs/stage1/DATA_CONTRACTS.md` |
| trading_status_normal_sh | trading_status | 600000.SH / SH | 2024-01-02..2024-01-05 | NORMAL | trading/suspension/ST/delisting 字段合同 | 未取得历史状态权威材料 | `docs/stage1/DATA_CONTRACTS.md` |

## 人工复核停点

本次没有选择或猜测任何新的事件证券、日期或事实。下列用途仍待逐项取得一手材料、固定
document-specific locator 和原始材料 SHA 后，生成新的 case/release 供用户确认：

| required purpose | candidate code/exchange | date range | expected authority | 当前决定 |
|---|---|---|---|---|
| NORMAL / SH-SZ-BJ calendar | 未选择 | 未选择 | SH/SZ/BJ 交易所日历 | 阻断 |
| BENCHMARK / 沪深指数 | 未选择 | 未选择 | 指数发布机构或交易所 | 阻断 |
| IPO / LISTING / POINT_IN_TIME | 未选择 | 未选择 | 交易所、监管机构、发行人公告 | 阻断 |
| DELISTING / DELISTING_PERIOD | 未选择 | 未选择 | 交易所决定及退市整理期公告 | 阻断 |
| ST / STAR_ST | 未选择 | 未选择 | 交易所及发行人风险警示公告 | 阻断 |
| SUSPENSION / RESUMPTION | 未选择 | 未选择 | 交易所停复牌公告 | 阻断 |
| LIMIT_UP / LIMIT_DOWN | 未选择 | 未选择 | 交易所规则与权威日值材料 | 阻断 |
| CORPORATE_ACTION | 未选择 | 未选择 | 发行人权益分派公告 | 阻断 |
| ADJUSTMENT_FORMULA | 未选择 | 未选择 | 供应商正式字段/算法合同及事件材料 | 阻断 |
| UNIT_SEMANTICS | 未选择 | 未选择 | 供应商正式字段合同 | 阻断 |
| MISSING_OR_UNSUPPORTED | 未选择 | 未选择 | 供应商正式能力合同及真实响应 | 阻断 |

用户尚未确认任何 v3 事件样本事实。不得把此表中的“未选择”替换为记忆、搜索摘要、博客、
论坛或供应商返回值；如后续需要访问官方网页，须先逐项展示精确 URL、访问目的、拟保存事实
字段和副作用并取得授权。

## Reference bundle

受管 bundle 文件集合为：

- `references/<reference_id>.json`：authority、PRIMARY tier、document-specific HTTPS
  locator、document ID、published/retrieved date 和原始材料 SHA。
- `facts.json`：稳定 fact ID、reference ID、dataset、purpose、code/exchange、起止日期和
  最短必要结构化 statement。
- `bindings.json`：精确 case ID → fact ID 映射。
- `manifest.json`：v3/v2 SHA 链、文件 inventory SHA、code/schema version、reviewed_at、
  reviewer decision/key/signature 和 manifest 自 SHA。
- `COMPLETE`：只绑定 manifest SHA。

verifier 复验规范 JSON、确定性排序、精确字段/文件集合、逐文件大小/SHA、manifest 自 SHA、
`COMPLETE`、受管相对路径、symlink/junction/reparse point，以及 reference/fact/case 的
dataset、purpose、code、exchange 和完整日期范围。缺失、多余、重复、首页、第三方摘要、
路径逃逸、无关事实复用、locator/material SHA/reviewer decision 篡改均失败；失败记录使用
独占新文件，仅含稳定脱敏 error code，不覆盖旧取证。

## REAL runtime readiness

固定 registry 只包含四个 enum source 及仓库内固定 module/factory/distribution 名称；调用方
不能传模块名、类名、SQL、SDK path 或任意数据路径。plan 不导入 SDK、不联网，冻结：

- source、dataset、v3 version/SHA、case/request/fields 和输出根；
- adapter/package/code/schema/rule/threshold/config version；
- network/QMT 需求、不可避免副作用和当前阻断原因。

版本合同：

| 凭证 | 冻结值/来源 |
|---|---|
| adapter | `xtquant-readonly.s1-07b.v1` / `baostock-readonly.s1-07b.v1` / `akshare-readonly.s1-07b.v1` / `tushare-readonly.s1-07b.v1` |
| provider package | 仅由 `importlib.metadata` 本机只读探针取得精确版本；缺失时为 `unavailable` 并阻断 |
| code | `s1-07b.runtime.v1` |
| schema | 各 dataset 的受控 Canonical descriptor version |
| rule | `quality-rules.v1` |
| threshold | `comparison-thresholds.v1` |
| sample | `s1-07.fixed-sample.v3` / `5b5bf202e17181dfcbd9159f261dc9b4c636fb7641a3786523b42f952c6f3200` |
| config | `real-validation-config.v1` |

本次本机只读 metadata 结果：XTQuant、BaoStock、AKShare、Tushare 均为 `unavailable`。
未安装、升级或导入任何供应商包。

R1 后 execute 的机器前置门与授权时序固定为：

1. exact acknowledgement；
2. 重新生成当前 plan；
3. 在 claim/import/session/evidence 之前检查 `PRODUCTION_ELIGIBLE`、非空
   source×dataset case scope、精确稳定 provider package version、安全绝对 output root，
   以及 adapter/package/code/schema/rule/threshold/sample/config 完整性；
4. 检查 `RuntimeAuthorizationEnvelope` 的 plan SHA、签名、scope/expiry、sample SHA 和
   全部冻结版本；
5. 使用 S1-07A 的跨进程持久 claim 原子消费 authorization；
6. 从受控 secret runtime 加载 REAL signer；
7. 从固定 registry 延迟加载 runner；
8. 创建 provider session 和不可变 evidence。

前置门错误码为：缺失、空值、`unavailable`、`unknown`、`latest` 使用
`PROVIDER_PACKAGE_VERSION_UNRESOLVED`；版本范围、预发布、本地版本或不可解析值使用
`PROVIDER_PACKAGE_VERSION_INVALID`；空 case 使用 `SAMPLE_SCOPE_EMPTY`；任一冻结版本或
sample SHA 漂移使用 `RUNTIME_VERSION_MISMATCH`；非绝对路径、`..`、symlink、junction 或
reparse point 使用 `OUTPUT_ROOT_UNSAFE`。这些失败均不消费 authorization，也不导入
signer、runner 或 SDK。全部前置门通过后才消费 authorization；此后 REAL signer/runner
初始化失败也不释放授权。

S1-07B 没有 production reviewer key、REAL signer key 或 runner enablement，因此在任何 SDK
import 前稳定阻断。Fake/test/self-signed key 永远不在 production allowlist；密钥不进入
命令行、环境回显、manifest、日志或 Git。

## 最终状态

| source | datasets | assessment | REAL 稳定运行数 |
|---|---|---|---:|
| XTQUANT | 六类 | BLOCKED_EXTERNAL_VALIDATION | 0 |
| BAOSTOCK | 六类 | BLOCKED_EXTERNAL_VALIDATION | 0 |
| AKSHARE | 六类 | BLOCKED_EXTERNAL_VALIDATION | 0 |
| TUSHARE | 六类 | BLOCKED_EXTERNAL_VALIDATION | 0 |

PRIMARY/FALLBACK 角色未变，全部 `auto_switch=False`。没有生产 DifferenceReport、
SourceOverride 或 DatasetVersion。

## 验证与副作用

- S1-07B-R1 攻击测试覆盖未解析/不稳定 package、全部冻结版本漂移、空 scope、不安全输出根、
  claim/import 计数和并发单次消费：`27 passed`。
- S1-07B-R1 加既有 S1-07B runtime 与文档状态定向组合：`105 passed`。
- S1-07A R2～R6、全部 validation、assessment repository、DataCenter、migration 和 Stage 1
  文档扩展回归：`291 passed`。
- 完整 `scripts/quality.ps1` 退出码为 0：离线后端
  `1160 passed, 2 skipped, 3 deselected`；Ruff、format、mypy、架构边界和 Markdown
  全部通过；前端单测 `17 passed`，生产构建通过，Playwright `1 passed`。
- 第一次完整门禁在测试前因沙箱不能读取既有离线 uv 缓存退出；取得只读许可后首次代码门
  在 Ruff format 发现一个文档状态测试需机械格式化，修正后原样重跑全绿。该缓存访问没有
  联网或下载依赖。
- Alembic 唯一 head 仍为 `20260729_0012`；S1-07B 没有新增 migration。
- 外部访问：无。只读取仓库、pytest 临时目录和本机 Python distribution metadata；未读取
  `E:\QuantKing`、`E:\QuantKingV3Data` 或 `F:\QuantKingV3Backups`。
- 副作用仅限仓库源码、测试、文档、离线临时测试产物，以及完整质量门对仓库内
  `frontend/node_modules` 和 `frontend/dist` 的常规刷新；没有真实数据、官方原始材料或凭据。
