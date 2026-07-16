# S0-05：备份、增量复制与恢复契约

状态：离线完整性门及两次经单独授权的
`E:\QuantKingV3Data` / `F:\QuantKingV3Backups` 合成恢复演练均已通过。S0-05 与
S0-07 的不可变运行目录和结果文件已保留。

## 边界与工具

- 固定实现工具为锁定 Python 3.12 标准库：SQLite 使用
  `sqlite3.Connection.backup`，普通文件使用流式读取/独占创建，发布使用同卷
  `os.rename`。不依赖 Robocopy 的镜像语义。
- 默认测试只访问 pytest 临时目录，不访问网络、QMT、真实用户数据或两个正式根。
- 真实演练只使用新建的 `acceptance/<run-id>` 合成目录。运行前必须重新取得授权并展示
  两个根、唯一 run ID 和副作用；失败后不得复用该 run ID。
- 任一托管路径的符号链接、junction/reparse point 或解析逃逸都会被拒绝；内容寻址对象必须是
  普通文件。
- 本任务没有删除、覆盖、镜像同步或保留清理代码。保留清理是后续独立、显式、可审计的任务。

Windows 本地临时目录已完成离线实测。F 盘的文件系统、ACL、容量和原子重命名证据仍必须由一次
经授权的真实合成演练产生，不能用离线结果代替。

## 恢复点格式

本地恢复点先写 `<run-id>.incomplete`，仅在两次严格验证通过后原子重命名为
`<run-id>`：

```text
<run-id>/
  input.json
  manifest.json
  payload/
    database/quantking.db
    config/...
    manifests/...
    partitions/...
  COMPLETE
```

`input.json` 固定源相对路径、目标相对路径和类别，不记录绝对源路径。
`manifest.json` 固定 schema 版本、恢复点 ID、UTC 创建时间、Git 代码版本、SQLite
`user_version`、Alembic 版本，以及每个实际 payload 文件的类别、字节数和 SHA-256。
所有 Stage 0 元数据和标记都使用严格解析的 `schema_version="1"`；字段缺失、未知字段、
旧版仅含 manifest 哈希的标记和其他 schema 版本都明确拒绝，不做静默降级。
本地 `COMPLETE` 的精确字段为 `schema_version`、`artifact_kind=local_snapshot`、
`restore_point_id`、`manifest_sha256` 和 `input_sha256`，同时绑定恢复点 ID、
`manifest.json` 与 `input.json`。

验证要求实际文件集合与 manifest 完全相等；缺失文件和额外文件都失败。数据库只能由在线备份
产生，完成后的检查使用 `mode=ro&immutable=1`，执行 `PRAGMA quick_check` 并核对
`user_version` 和唯一 Alembic 版本。验证前后均不得出现 `-wal` 或 `-shm`。

失败目录保留原 ID 加 `.incomplete` 后缀，包含已写入的 `input.json` 和
`failure.json`；可能已出现的完成/验证标记会改名为失败证据，目录不得删除、修改、复用或补成
成功。修复必须使用新 ID 产生新目录。

## 异盘增量复制

异盘存储为追加式内容寻址对象和不可变恢复点引用：

```text
<offsite-root>/
  objects/sha256/<digest-prefix>/<sha256>
  objects/.incomplete/<restore-point-id>/<sha256>
  restore_points/<restore-point-id>/
    input.json
    manifest.json
    replication-report.json
    COMPLETE
```

`replication-report.json` 同样固定 `schema_version="1"`，其 `restore_point_id` 必须与
恢复点目录和 `COMPLETE` 一致；`state=complete` 时
`copied + skipped + conflicted = total_objects`、`conflicted=0`，且总数必须等于 manifest
去重后的对象数。异盘 `COMPLETE` 的精确字段在本地字段基础上增加
`source_restore_point_id` 和 `replication_report_sha256`，因此同时绑定 manifest、input、
复制报告、异盘恢复点 ID 和源快照 ID。复制报告内容、计数或 ID 被篡改，即使重新签名
`COMPLETE` 也会失败。

复制前先完整验证本地恢复点。增量决策只使用 manifest 中的大小和 SHA-256：

1. 对相同 SHA-256 去重，并按 digest 稳定排序。
2. 检查所有目标对象和临时对象路径没有 reparse/解析逃逸。
3. 按目标端缺失对象的总字节数做容量预检；不足时保留失败恢复点证据且不复制对象。
4. 已存在且大小/哈希相等的对象计入 `skipped`；缺失对象流式复制、复核后原子发布并计入
   `copied`；同 digest 路径存在不同内容时计入 `conflicted`，绝不覆盖。
5. 报告 `total_objects/copied/skipped/conflicted` 的实际计数；只有零冲突、源再次验证通过、
   对象集合验证通过后才写 `COMPLETE` 并发布恢复点引用。

重复复制相同恢复点应为全部 `skipped`；只变更一个分区时应只新增一个对象。历史恢复点、失败
目录和无关文件都必须保持字节不变。

## 隔离恢复

本地或异盘恢复只能写入新的 `<restore-id>.incomplete`，不能恢复到运行根或既有目录：

1. 验证源 `COMPLETE`、manifest、精确文件集和对象哈希。
2. 按 manifest payload 总字节数做目标容量预检。
3. 流式复制到新隔离目录，重新验证精确文件集、大小和 SHA-256。
4. 用不可变只读 SQLite 连接执行 `quick_check`，核对 schema/Alembic 版本，并确认没有
   WAL/SHM 副产物。
5. 异盘恢复还把被 `COMPLETE` 绑定的复制报告保存为
   `source-replication-report.json`。写入版本化 `VERIFIED`，精确绑定恢复请求、input、
   manifest、源 `COMPLETE`、可选源复制报告的 SHA-256，以及恢复 ID、源恢复点 ID、源快照
   ID、来源、验证时间和 `quick_check=ok`；再次按精确字段、schema、ID、所有绑定哈希与
   payload/SQLite 完整性验证后才原子发布。已发布隔离恢复可使用同一验证入口重新复核。

任一检查失败只留下 `.incomplete`、`restore-request.json`、已有输入和
`failure.json`，不得出现成功的 `VERIFIED`。

## 运行基线

以下为 Stage 0 已验收的运维基线；未来用于正式业务数据仍须按对应任务取得授权：

- 频率：每次正式发布后和数据库迁移前创建本地恢复点；有状态变化的交易日结束后至少一次。
  本地验证成功后当天复制到异盘，周度调仓前再确认最近一次异盘恢复点可验证。
- 保留：建议本地保留最近 14 个日恢复点和 8 个周恢复点；异盘保留最近 30 个日恢复点和
  12 个自然月月末恢复点。当前代码不执行此策略，也不回收内容寻址对象。
- 访问控制：数据根和异盘根只授予运行用户、`SYSTEM` 和本机 Administrators 所需权限；
  不向普通用户开放写入，不通过宽泛共享或继承 ACL 弱化失败/正式产物的不可变语义。
- 容量：本地快照按声明源、SQLite WAL/SHM 和至少 1 MiB 余量做保守预检；异盘按缺失对象
  总大小预检；恢复按 manifest payload 总大小预检。Windows/F 盘真实可用空间仍以运行前
  预检和真实演练证据为准。

## 验收命令与证据

默认离线命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\backup -m "not real_backup" -q -p no:cacheprovider
```

2026-07-16 复跑结果为 `42 passed, 1 deselected`；deselected 项是唯一
`real_backup` 标记，没有访问正式根。

只有重新取得明确授权后，才可执行以下命令；命令中的 run ID 必须全新：

```powershell
.\.venv\Scripts\python.exe scripts\run_backup_acceptance.py `
  --data-root E:\QuantKingV3Data `
  --offsite-root F:\QuantKingV3Backups `
  --run-id s0-05-YYYYMMDD-HHMMSS
```

成功证据固定在
`E:\QuantKingV3Data\acceptance\<run-id>\acceptance-result.json`、两个本地快照、
本地/异盘隔离恢复目录和
`F:\QuantKingV3Backups\acceptance\<run-id>\store\restore_points`。报告必须证明首轮全复制、
重复轮全跳过、单分区变化只复制一个对象、两种恢复均为 `VERIFIED`、历史/失败目录未删除。
每个新 run 还会在自身异盘目录内预置人工历史恢复点、失败目录和无关哨兵；结果报告保存操作
前后精确文件清单（相对路径、字节数、SHA-256）及新增、删除、修改、保留集合与计数。
`zero_deletes` 和 `zero_modifications` 由该差异计算得出，不是常量；任一为假都会阻止成功报告。

顶层失败固定写入该唯一数据 run 目录的 `acceptance-failure.json`；内部 snapshot、复制或恢复
失败还保留各自 `.incomplete/failure.json`。任何失败都会阻止成功证据生成，重试必须再次授权并
使用新 run ID。

2026-07-16 经分别授权完成两个运行：`stage0-s05-20260715-1920` 与
`stage0-s07-20260715-1921`。两次均为首轮 `copied=5, skipped=0`，重复轮
`copied=0, skipped=5`，单分区变化轮 `copied=1, skipped=4`；本地与异盘隔离恢复通过，
预置的历史点、失败目录和哨兵均为 `deleted=0, modified=0, preserved=3`。完整路径与代码版本见
[最终验收报告](FINAL_ACCEPTANCE.md)。
