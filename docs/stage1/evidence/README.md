# S1-07K3 真实执行证据目录

本目录保存 K3B 真实探针的失败与诊断证据。这些文件是不可变事实记录，只追加、不原地修改；
新增证据使用新文件名。

| 文件 | 内容 |
|---|---|
| `k3b_representative_probe_attempt_2026-08-13.json` | V2 代表性 marker 第一次尝试（VPN 开启）：BaoStock/000001.SZ 成功，东财断连即停，余 22 次未调用 |
| `k3b_representative_probe_attempt_2026-08-13_b.json` | 第二次尝试（VPN 关闭）：同结果，附 DNS/TCP 诊断（网络层全通、HTTP 层被断） |
| `k3b_connectivity_probe_2026-08-13.json` | 最小连通性探针（东财/腾讯/新浪各 1 次）：腾讯、新浪 complete，仅东财 FAILED |
| `k3b_eastmoney_ipv6_root_cause_2026-08-13.json` | 根因证据：同一 curl 请求 IPv4 返回 200 完整数据、IPv6 数据断流（curl 56）；ADR-019 依据 |
| `k3b_eastmoney_throttling_2026-08-13.json` | 限流证据：IPv4 钉死生效后，同一 curl 请求从 200 变为 56，python-requests 与 .NET TLS 栈同断 |
