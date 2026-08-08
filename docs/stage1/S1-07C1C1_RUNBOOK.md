# S1-07C1C1 XTData observation 逐次授权操作手册

## P0 边界

本手册对应 Stage 1 / S1-07C1C1-P0。P0 只准备、签发和 review 仓库外的授权材料，**不启动
MiniQMT**，不检查、定位或导入本机真实 XTQuant，不访问网络、QMT、正式数据根或旧项目，也不执行
REAL marker。

唯一正式入口是 `scripts/run_s1_07c1c0.py`；它只有以下四个子命令。不得使用 DTO、环境变量或
命令行字段构造、修改或替代授权。

```powershell
uv run python scripts/run_s1_07c1c0.py prepare `
  --c1b-authorization-file <external-c1b-authorization.json> `
  --plan-file <external-c1c0-plan.json>

uv run python scripts/run_s1_07c1c0.py issue `
  --plan-file <external-c1c0-plan.json> `
  --authorization-file <external-c1c0-authorization.json>

uv run python scripts/run_s1_07c1c0.py review `
  --authorization-file <external-c1c0-authorization.json>

uv run python scripts/run_s1_07c1c0.py execute `
  --authorization-file <external-c1c0-authorization.json> `
  --ack I_ACKNOWLEDGE_C1C0_R3_AUTHORIZED_XTDATA_ST_OBSERVATION
```

`prepare` 只能接受 C1B 正式 canonical authorization 文件，并在写入 C1C0 plan 前执行 C1B
live extension precheck。`issue` 只能读取 C1C0 canonical plan。plan 和 authorization 均必须在
Git 仓库外，且目标不得已存在或经过 symlink/reparse point。`review` 与 `execute` 也只读取仓库外
canonical C1C0 authorization；C1B 文件不能调度 C1C0，C1C0 文件也不能进入 C1B。

## 未来单次真实执行前的人工确认

未来另获单次真实执行授权时，用户必须先在系统外人工确认 MiniQMT 已启动，且处于预期账户和预期
环境。该确认不由脚本探测、推断或替代。

随后运行 `review`，并逐项核对下列输出后才可以发出单次书面授权：

- authorization SHA、invocation ID 与 authorization ID；
- source、dataset、case；
- site-packages root、output root 与 claim root；
- XTQuant package version；
- valid_from 与 expires_at；
- 唯一固定调用 `xtdata.get_his_st_data("603377.SH")`。

review 同时显示 C1B authorization SHA、plan SHA、assessment/session/run ID、reference bundle ID/SHA、
metadata database SHA、runner contract/profile/version、authorization/runner key ID、精确 execute
acknowledgement、允许结论和 `real_execution_authorized=false`。它不显示私钥内容或路径、claim nonce、
authorization/runner signature、SDK 响应或其他机密内容。

授权模板如下；占位符必须逐项替换为同一份 review 的值：

> 授权执行 S1-07C1C1 单次真实只读 invocation：
> authorization_sha256=<sha>，
> invocation_id=<id>，
> authorization_id=<id>，
> source=XTQUANT，
> dataset=trading_status，
> case=st_warning_603377_sh，
> site_packages_root=<path>，
> output_root=<path>，
> claim_root=<path>，
> expires_at=<time>。
> 仅允许调用 xtdata.get_his_st_data("603377.SH") 一次；
> 禁止 xttrader、交易、账户、订单、下载、订阅、其他 case 和自动重试。

该模板本身不构成授权。只有用户针对刚 review 的精确值明确授权后，才可使用完全相同的
authorization 文件与精确 acknowledgement 调用 `execute`。

## 失败与发现处理

- authorization 失败、过期或被拒绝后，必须重新生成全部 ID 和 authorization；不得修补或复用旧文件。
- claim 后任何失败均不得重试原 authorization；claim 已消耗后必须走新的逐次授权流程。
- 方法不存在、MiniQMT 不可用、空响应和解析失败均是合法发现，不能通过改写响应或补造数据来取得
  `READY`。
- 该 case 的唯一允许结论始终是 `BLOCKED/CONTRACT_INCOMPLETE`；不得将 observation 解释为真实
  能力证明、production capability 或 `READY`。
