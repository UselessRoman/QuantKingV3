# S0-02 联合兼容性报告

状态：2026-07-15 锁定环境中的离线联合探针已通过；同日经本任务明确授权，真实
XTQuant 最小只读探针也已通过。主 agent 已独立复核 S0-02 的测试、实现、边界和证据，
因此 S0-02 单卡已验收；这不改变 Stage 0“实施中”、Stage 1“未开始”的状态，S0-07
仍须复跑本报告中的验收门。

## 范围与边界

本任务只验证 Python 3.12 锁定环境和第三方扩展点，不实现数据仓、研究数据集、模型
训练业务、组合服务或正式回测。所有离线文件均位于临时目录，没有访问网络、QMT、
`E:\QuantKingV3Data` 或 `F:\QuantKingV3Backups`。

FastAPI 探针位于明确的 `web/adapters/compatibility.py`，只创建临时应用和临时健康
端点供 `TestClient` 调用。它不是生产应用工厂，也不代表 S0-04 Web 能力已经实现；
S0-04 必须独立建立正式应用入口。

## 离线实测

执行环境为 Windows AMD64、CPython 3.12.13。实际命令：

```powershell
. .\scripts\activate-toolchain.ps1
$env:UV_OFFLINE = "1"
.\.venv\Scripts\python.exe scripts\run_compatibility_probe.py
```

命令退出码为 `0`，报告状态为 `passed`，六个子项记录了同一个进程 ID。实测版本和
功能证据如下；时长只写入每次 JSON 报告，不作为稳定验收阈值。

| 子项 | 实测版本 | 实际执行证据 | 结果 |
|---|---:|---|---|
| SQLite | 3.53.1 | 临时库启用 WAL，建表写入、读取、`quick_check` | 通过 |
| DuckDB | 1.5.4 | 创建临时库后以 `read_only=True` 重开并查询 | 通过 |
| LightGBM | 4.6.0 | 固定种子、单线程、极小合成数据训练和重复预测 | 通过 |
| Backtrader | 1.9.78.123 | 三根合成 bar、无订单 Strategy、现金不变 | 通过 |
| FastAPI | 0.139.0 | 临时应用由进程内 `TestClient` 请求健康端点 | 通过 |
| pyqlib | 0.9.7 | 版本/接口、合成目标权重、分数独立性、AST 门 | 通过 |

定向自动测试命令和结果：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\compatibility `
  -m "not real_xtquant" -q -p no:cacheprovider
```

结果为 `21 passed, 1 deselected`。加入架构边界测试后为
`24 passed, 1 deselected`。锁定依赖会输出 Qlib/Gym 和 FastAPI/Starlette 的上游弃用
告警；它们写入标准错误，不污染脚本标准输出中的 JSON，也未改变本次功能结果。

脚本默认把完整报告写到标准输出。只有用户显式给出 `--output` 时才写文件；该参数必须
是当前受管理工作目录下的相对 `.json` 路径，脚本不会创建正式运行目录。例如：

```powershell
.\.venv\Scripts\python.exe scripts\run_compatibility_probe.py `
  --output compatibility-report.json
```

每个结果和总报告都是不可变类型，包含版本、稳定步骤名、毫秒时长、进程 ID、状态和
稳定错误码。未知第三方异常统一转换为 `execution_failed` 与固定消息；JSON 不包含异常
堆栈、异常原文或文件路径。任一子项失败时仍执行并报告其余子项，最终以非零状态退出。

## Qlib Stage 4 扩展点记录

Stage 4 应基于锁定 pyqlib 0.9.7 的
`qlib.contrib.strategy.signal_strategy.WeightStrategyBase` 实现 QuantKing 适配器，并锁定
以下扩展方法：

```text
generate_target_weight_position(
    score, current, trade_start_time, trade_end_time
)
```

选择依据：该钩子直接返回目标权重映射，允许 Stage 4 把 QuantKing 已生成的
`TargetPortfolio` 作为唯一权重来源，再由 Qlib 的订单生成层消费。S0-02 的探针子类只按
交易日期返回预置合成权重，完全忽略 `score` 和 `current`；任意修改分数不会改变输出。
非法权重、总和超过 1 和缺少交易日期都稳定失败。源码 AST 检查证明探针子类只继承
`WeightStrategyBase`，没有引用 `TopkDropoutStrategy` 或其他内置 Strategy 组合构造器。

限制：这里没有初始化 Qlib 数据仓、交易所、正式信号、订单生成或回测，也没有实现
Stage 4 的组合规则。Stage 4 仍须把正式不可变 `TargetPortfolio` 适配到该钩子，并独立
验证交易日对齐、现金、成本和双引擎语义。升级 pyqlib 时必须重新验证版本、方法签名、
分数独立性和 AST 门，不得假定 0.9.7 的接口继续兼容。

## XTQuant 最小只读实测

真实 XTQuant 结果：**经明确授权已通过**。授权和执行仅对 2026-07-15 这一次命令有效；
以后重跑仍须取得新的明确授权。

真实测试只从 `QUANTKING_XTQUANT_SITE_PACKAGES` 指定的绝对 site-packages 根读取。执行前
测试会打印该根、只读范围和零写入/零连接副作用。探针先确认 `xtquant` 包和 cp312 原生
文件均位于该根；在导入前阻断 `requests.get`、socket 外联和文件写入；随后只导入
`xtquant` 与 `xtquant.xtdata`，记录版本并检查 `get_market_data_ex` 可调用。它不调用该
函数，不连接 QMT，不获取 client，不读取或下载行情，也不导入交易模块。

本次授权的输入根为 `E:\Python\Lib\site-packages`，独立运行命令为：

```powershell
$env:QUANTKING_XTQUANT_SITE_PACKAGES = "E:\Python\Lib\site-packages"
.\.venv\Scripts\python.exe -m pytest `
  tests\compatibility\test_xtquant_real.py -m real_xtquant -s `
  -q -p no:cacheprovider
```

退出码为 `0`，结果为 `1 passed`；另有两个来自外部 SDK 传递模块的弃用告警，不影响
边界或结果。标准输出在测试前打印了上述绝对根、只读范围和零写入/零连接副作用。
探针确认该根包含 CPython 3.12 原生模块，导入的 `xtquant`/`xtquant.xtdata` 均来自该根，
`get_market_data_ex` 存在且可调用，并且交易模块未导入。它没有调用行情函数，因此该结果
只证明锁定解释器下的最小只读接口兼容性，不证明 QMT 连通、行情可用或账户能力。

默认离线质量门继续使用 `not real_xtquant`，不会隐式重跑真实探针。S0-07 复跑或失败后
的任何重试都需要重新取得授权。
