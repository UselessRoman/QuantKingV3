# S2-01 本地候选独立复核

2026-09-09，独立子代理 `review_s201`，只读。最初按任务卡读取本地研究合同、样本、范围、
来源报告、探针计划与 R3 diff；随后针对主代理修复的三文件（合同源码、合同测试、合同说明）
复验固定锚点问题。没有修改文件、外部调用或完整门禁运行。

结果：锚点当日因子非 1、同证券跨日换锚两个缺口已关闭；源码、测试和合同一致，未发现新的
本地阻断。来源资格、正式范围与 S2-02 构建器覆盖校验仍未完成，不属于本复核的通过结论。

以下是实际执行的单次 Python 反例命令，退出码 0，5 个场景符合预期（3 拒绝、2 接受），
**不是 5 条 pytest 测试**。

```powershell
. .\scripts\activate-toolchain.ps1 -UvCacheDirectory .uv-cache
$reviewCode = @'
from datetime import date
from decimal import Decimal
from dataclasses import replace
from quantking.common.time import UtcTimestamp
from quantking.research.contracts import SourceFact, AdjustmentFactorDaily, validate_reference_rows, ResearchContractError

stamp = UtcTimestamp.from_iso('2026-01-13T07:00:00Z')
source = SourceFact('synthetic', 'v1', 'a' * 64, stamp, stamp)
first = AdjustmentFactorDaily('000001.SZ', date(2026, 1, 9), date(2026, 1, 9), Decimal(1), source)
changed = AdjustmentFactorDaily('000001.SZ', date(2026, 1, 12), date(2026, 1, 12), Decimal(1), source)

for rows in ((first, changed), (changed, first)):
    try:
        validate_reference_rows(rows, as_of=stamp)
    except ResearchContractError as exc:
        assert str(exc) == 'CONFLICTING_FACTOR_ANCHOR', str(exc)
    else:
        raise AssertionError('mixed anchors accepted')

try:
    AdjustmentFactorDaily('000001.SZ', date(2026, 1, 9), date(2026, 1, 9), Decimal(2), source)
except ResearchContractError as exc:
    assert str(exc) == 'FACTOR_ANCHOR_NOT_NORMALIZED', str(exc)
else:
    raise AssertionError('non-unit anchor accepted')

stable = replace(changed, anchor_date=first.anchor_date, cumulative_factor=Decimal(2))
other = replace(changed, code='600000.SH')
for rows in ((first, stable, other), (other, stable, first)):
    validate_reference_rows(rows, as_of=stamp)

print('PASS: both original counterexamples rejected; stable factors, per-security anchors, and reversed input accepted')
'@
.\scripts\uv.ps1 run --offline --frozen python -c $reviewCode
```

实际输出：

```text
PASS: both original counterexamples rejected; stable factors, per-security anchors, and reversed input accepted
```

完整质量门由主代理独立执行，命令和结果见 [S2-01 实际验收](S2-01_ACCEPTANCE.md)。

## 继续开发后的任务卡复核

2026-09-09，独立子代理 `s201_acceptance_audit` 只读核对任务卡、当前合同、规则、fixture、
验收及 ADR-029，未发现可证实的新本地合同缺陷。固定工具链运行：

```powershell
. .\scripts\activate-toolchain.ps1 -UvCacheDirectory .uv-cache
.\scripts\uv.ps1 run --offline --frozen pytest tests/research -q --tb=short
```

实际结果 **18 passed in 0.12s**。没有修改文件、联网、访问正式/旧根或运行完整门禁。
逐键覆盖、生效日期消费校验和父 PUBLISHED/文件验证已有明确 S2-02 归属，不误报为当前新缺陷。
同时确认来源资格与正式范围仍未满足；负面资格报告不能代替“影响 S2-02 的决定全部冻结”。
fixture 当前只冻结独立预期，不证明尚未实现的构建器已通过未来扰动测试。

同一独立代理随后只读复核 a1 执行器修复，运行
`pytest tests/data/adapters/test_research_probe.py -q --tb=short`，**14 passed in 6.78s**。
核对服务端页容量、连接异常无后续发送、部分发送不重试、解压尾部拒绝、首错无 logout、
父进程限时队列与完整 IPC 读取线程。尚无专门 IPC 半包挂起注入测试，该项是静态复核与一般
阻塞超时回归，不能报告成专门注入测试通过。未发现这组修复的新可证缺陷。
另外独立复算 a1 清单 21 文件的 SHA、大小和目录集合全部一致，冻结执行器/uv.lock 摘要均与
当前文件匹配；18 次开始/完成/请求意图、1 连接、7.187 秒、75,235 字节与原报告一致。
该复验补足此前额度不足未完成的独立修复复核；不重跑 a1，也不等同来源资格通过。

独立代理随后只读核对 a2：认可 688469 上市日、002499 退市整理起始与预计末日、SH 计划休市
原文；明确未证明精确 available_at、实际摘牌日或 SZ 日历。首批混合搜索不能拆出各查询命中率，
三个可读原文与两个工具取文错误、13 操作/6 工具调用均和保存记录一致。未联网或修改证据。

2026-09-10，同一独立代理只读复核 a3 成功原文及 a1 对应字段：确认平安现金税前金额与登记、
除权、结算代派日期一致，净金额需区分主体和持有期；浦发转债公告不能证明股息支付日或股票
参考价；科林已支持实际摘牌日 04-18，但无精确历史 available_at。会议日与 dividAgmPumDate
不同不等于供应商错一天，平安正文身份与外层旧标题应分列。未联网、修改或重跑。
