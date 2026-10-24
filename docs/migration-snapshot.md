# 严格迁移快照与写屏障（B02，未投入生产）

`user_export` 是一键用户备份，不是可迁移快照。迁移/恢复前置需要：

- 同一 epoch 下的多库一致水位；
- 维护元数据、catalog/job/manifest、删除 ledger/墓碑/outbox；
- 密钥材料同代可验证；
- 可信维护控制器冻结写入，而不是逐库备份后假称全局原子；
- 隔离恢复演练通过后才允许生产迁移。

## 当前实现

`core/snapshot_barrier.py` 定义 `SnapshotBarrier` 协议和不可变
`SnapshotMarker`。`BackupService.start(backup_type="migration_snapshot",
barrier=...)` 才生成严格快照；不传可信 barrier 会直接拒绝：

```text
barrier_required
```

严格模式与用户导出的区别：

- 额外包含 `maintenance.sqlite3`、整个 `maintenance/`（可能有 deletion
  ledger/隔离元数据）和 `compatibility_state.json`（存在时）；
- 必需项缺失、SQLite backup API 失败、quick_check 失败、文件在快照期间变化、
  凭据无法验证都会失败，**不会** raw-copy 兜底或发布部分包；
- ZIP 写入后先用 `core.restore.verify_backup` 做 CRC/成员/hash/SQLite/密钥
  复验，再原子发布；
- manifest 记录 barrier 的 epoch/generation/high_water/contract_epoch、
  `snapshot.kind`、`deletion_ledger_present` 和阻塞原因；
- `capabilities.cross_plane_consistent` 只有在所有 SQLite 都一致性快照时为
  true。

严格快照会对每个 `data/memory/*` SQLite 快照执行只读 D01 语义探测，并把
schema 版本、`M001` 迁移标记、per-persona `next_seq/append_revision/
mutation_gen/lifecycle_epoch`、history/tombstone/outbox 计数和 tombstone
指纹写入 manifest 的 `ledger` 块。该块有独立 SHA-256；verify/drill 会从
归档数据库重算并比对，manifest 不能自报通过。路径名含
`ledger/tombstone/deletion` 只记录为 `ledger_artifact_present`，不参与放行。

`migration_safe=true` 必须同时满足：

- 可信屏障返回的 `generation > 0` 且快照期间所有 SQLite 一致；
- 全部已发现 persona DB 的 ledger 证据 `aware=true`、`complete=true`，
  迁移后不变量成立；
- `compatibility_state.json` 的 `data_contract_epoch >= 1`、
  `migration_set_id=M001`、phase 不是迁移中/恢复中；
- 维护屏障的 `contract_epoch` / `migration_set_id` 与该状态完全一致。

否则保持 `migration_safe=false`，`migration_blocked_reason` 取
`semantic_deletion_ledger_required`、`ledger_evidence_incomplete`、
`contract_epoch_or_migration_set_mismatch` 或
`barrier_attestation_required`。屏障自报的 `migration_safe` 只记录、不采信。
这仍然**不是**生产迁移放行标志：还需真实 writer 注册/冻结、M001 生产执行、
隔离恢复演练和用户授权。

D01 之后的恢复先调用 `core.d01.plan_restore_replay(snapshot_db, source_db)`：
`exact` 可直接恢复；`replay_tombstones` 需先执行
`apply_tombstone_replay`；出现新消息、墓碑回退或 epoch/ledger 分叉时返回
`blocked`，禁止裸覆盖。重放不改写 raw JSON，只应用 tombstone/delete_ops/
outbox 并把受影响 summary 标为 stale。

## 维护控制器与 bootstrap permit

- `core/maintenance.py` 实现实例级维护状态/`flock`/writer 注册表：
  `idle → requesting → draining → quiescent → snapshotting → running`；
  失败保持 `recovery_required` 且不自动释放锁。
- `ControllerBarrier` 把控制器适配成严格快照 barrier；WebUI
  `RuntimeHost.start/restart` 先查维护 gate 和当前代码兼容 gate，不能绕过。
- `core/bootstrap.py` 只对 legacy `data_contract_epoch=0`、未启用
  tombstone/import/stable 能力的源生成 `bootstrap_epoch0` 严格快照；
  必须通过离线 verify + 隔离 drill 后，写入一次性 permit。
- permit 绑定源 inventory hash、目标 build、目标 manifest hash、显式
  `allowed_migration_ids`、期限和一次性 generation；不创建空 ledger，
  不把 `migration_safe` 改成 true。
- `consume_permit` 校验 permit 未消费、未过期、inventory/target/migration ID
  匹配；每次只能消费一次。

## 尚未完成（不能声称 B02 完成）

- writer 注册表目前是可注入的维护参与者；实际 history/GC/summary/RAG/配置/
  key 轮换等 Runtime writer 还没有全部注册，旁路 SQL/文件 writer 仍需测试发现。
- 没有把 maintenance/catalog/job/manifest/outbox 的语义水位接入 Runtime。
- 权威 registry 的“必需平面完整清单”尚未接入；当前完整性基于已发现 DB、
  目录遍历错误 fail-closed 和 strict partial 拒绝，仍不等于外部权威清单。
- D01 只有隔离合成库的迁移/ledger 模块，未在生产执行；恢复重放的 exact/
  tombstone-only 路径已有合成测试，但尚未接入 restore CLI 或一键恢复。
- 没有自动安装 launcher，也没有修改 systemd。
- 没有真实生产恢复演练；当前只能对合成包做离线 verify/drill。

生产迁移必须等 U01-L 独立 launcher、真实 writer 冻结、D01 ledger 和
隔离恢复演练全部闭环；在此之前保持 blocked。
