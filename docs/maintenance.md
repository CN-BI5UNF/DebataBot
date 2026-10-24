# 维护控制器（M00，未生产验收）

`core/maintenance.py` 提供实例级维护状态和 writer 屏障，状态文件位于
`<DATA_DIR>/maintenance/controller_state.json`（0600），锁为
`controller.lock`（POSIX `flock`）。状态只含 phase/generation/owner/purpose/
水位，不含聊天正文或密钥。

## 状态机

```text
idle → requesting → draining → quiescent → snapshotting/migrating
     → validating → ready_to_resume → running
异常 → recovery_required（保持锁，需人工恢复）
```

- `request`：非阻塞取得跨进程锁，进入 requesting，写新 generation/token。
- `drain`：调用已注册 writer 的 stop，未结算则失败并进入 recovery。
- `quiesce`：要求所有 writer 已结算，写 snapshot_hash/水位，返回
  `SnapshotMarker`。
- `resume`：调用 resume，进入 running，释放锁。
- `ControllerBarrier`：供严格快照使用；freeze 生成 marker，release 恢复。
  D01 之后的快照必须显式传入 `contract_epoch=1`、`migration_set_id="M001"`
  （或当前受控值）；它们会写入持久维护状态并进入 marker，供严格快照与
  `compatibility_state.json` 交叉校验。缺省仍沿用状态中的旧值，不能自报
  `migration_safe`。
- `startup_gate`：WebUI/RuntimeHost 启动前读取状态；维护 phase 禁止启动。
- `RuntimeHost.start/restart` 同时检查维护 gate 与当前源码兼容 gate。

## 边界

- writer 注册表目前是可注入的维护参与者；Runtime 的真实 history/GC/summary/
  RAG/配置 writer 尚未全部接入。未注册的旁路 SQL/文件写入不能被本模块
  神奇拦截。
- Windows 没有 `flock` 时仅进程内互斥，不承诺同等跨进程安全。
- 状态文件写失败/锁失败 fail-closed；不自动部署 launcher、不改 systemd。
- 生产维护操作需要单独授权；当前没有真实生产冻结/迁移。
