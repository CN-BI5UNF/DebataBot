# 代码/数据兼容门禁（U01）

目标：更新或回退源码前，先确认目标代码理解当前数据，避免旧代码忽略墓碑、
重排 seq 或从旧文件重新导入。门禁是**离线纯数据检查**，不执行目标 Python，
不迁移数据，不创建空库。

## compatibility.json

仓库根目录的 `compatibility.json` 随源码发布，至少包含：

- `manifest_format`：当前为 `1`；未知格式 fail-closed。
- `minimum_launcher_protocol`：目标要求的启动保护协议版本。
- `data_contract_epoch`：数据契约代次；迁移后必须递增。
- `migration_set_id`：迁移集标识；空表示基线。
- `irreversible`：是否包含不可逆迁移。
- `planes`：`root_config` / `persona_db` / `maintenance` / `rag` / `projection`
  各自的 `supported_read`、`supported_write`、`target_version`。
- `required_features`：目标代码依赖的数据能力，例如未来的
  `stable_seq`、`tombstone_read_barrier`、`asset_epoch`。
- `optional_features`：仅供展示/审计的能力。

当前基线（2026-10-24）：

```text
root_config=2, persona_db=1, maintenance=1, rag=0, projection=0
data_contract_epoch=0
```

D01 的隔离迁移目标约定为 `data_contract_epoch=1`、`migration_set_id=M001`；
生产 M001 完成前仓库基线仍保持 0，不能用普通启动/更新自动推进。

## 本地数据快照

`core/compat.py` 只读取元信息：

- `config.yaml` 的 `version`；
- `memory/*/*.db`、`vector/*/*.sqlite3`、`maintenance.sqlite3` 的
  `PRAGMA user_version` / `schema_version`，**逐个文件**登记路径、平面和错误；
- 可选的 `<数据目录>/compatibility_state.json`（state_format、phase、
  generation、epoch、迁移集、features、projection 版本）。目前代码不会自动
  创建它；D01 迁移/保护层负责写入。

SQLite 探针使用 `file:...?mode=ro` + `uri=True` + `PRAGMA query_only=ON`；源文件
消失时只返回 missing/error，不会新建空库。不读取聊天正文、摘要、密钥或媒体。

同一平面不再只用 max 版本：每个已登记 DB 都单独检查；一个旧库或新增异版本库
不会被另一个新库掩盖。已存在但不可读的数据库会让门禁失败
（`data_unreadable`）。`compatibility_state.json` 的 planes 不能升降实测版本；
只有没有独立探针的 `projection` 可由受控状态声明。

**版本方向（L00 规范）**：`supported_read`/`supported_write` 是目标代码支持
的**上界**，必须满足 `target_version <= supported_write <= supported_read`。
本地版本高于任一上界拒绝；低于 `target_version` 要求迁移；高于
`target_version` 是回退风险。launcher 与 core 使用同一规范，不能把
`supported_read` 当下限。

`compatibility_state.json` 的 `phase=preparing/running` 返回
`migration_in_progress`，`failed/recovery_required` 返回
`recovery_required`；这两种状态都禁止普通更新/启动。状态字段统一使用
`data_contract_epoch`（不再接受 `epoch` 作为另一种含义）；未知 phase、
非法 `generation`、未知 plane/feature 都 fail-closed。

## 首次运行与恢复

- **真正首次运行**：没有 config/state、没有任何已知库，且数据目录没有其他
  持久文件（`instance.json`/`root.json` 初始化清单除外）；返回
  `setup_required`，只允许向导，不能启动普通 Runtime/迁移。
- 没有 config 但已有 DB/state/维护物：返回 `recovery_required`，不能当作
  首次安装重新初始化。
- 刚完成向导、尚无任何库的空实例：core 的 Runtime 入口允许初始化空库；
  launcher 仍把 `setup_required` 与普通启动分开，需显式 `--allow-setup`。
- 逐库不可读、符号链接、未知 phase/generation 都 fail-closed；不自动创建
  空库，不以 `immutable=1` 忽略活动 WAL。

## 更新器行为

`UpdateService.check` / `apply` 都调用同一套预检：

- 无 `compatibility.json` 的 legacy tag：列表可见，但 `can_apply=false`，
  `legacy_unknown`；不能用普通回退按钮绕过。
- 目标目标版本高于本地：`migration_required`；自动更新不会执行迁移。
- 目标不能读写本地版本：`unsupported_data_contract`。
- 本地 epoch 高于目标：`data_contract_too_new`。
- 缺少 required feature：`feature_missing`。
- 检查后本地数据版本/迁移状态变化：`data_state_changed`，需要重新检查。
- 目标 manifest 变化：`compat_manifest_changed`。

UI 会显示“兼容性未通过”，禁用该版本的自动应用按钮，并解释原因。绿标也只针对
可向前推进且通过门禁的 Release。

## 独立启动保护（代码已准备，未部署）

2.4 要求把最终门禁放到**不会被 git switch 覆盖**的受信 launcher/服务前置程序，
在 import 目标 Runtime 之前检查 manifest 与本地 contract。代码和 systemd
样例在 `deploy/trusted_launcher.py`、`deploy/README.md`：

```bash
python3 deploy/trusted_launcher.py \
  --project-root /path/to/DebataBot \
  --data-dir /path/to/instances/default \
  --check-only --json
```

launcher 只依赖 Python 标准库，不 import 项目模块；失败退出 78，不启动目标。
它和 core 使用同一上界语义、同一 `data_contract_epoch`、逐库探针和首运行/
恢复规则；`tests/test_compat_parity.py` 对同一合成状态做 golden 差分。
launcher 还检查维护 controller 状态，并绑定：

- `--project-root`、`--data-dir` 和可选的 `--config`；
- 目标 executable/入口必须在 project root 内，且入口为 `main.py`；
- 命令中的 `--config` 必须与 launcher 检查的配置一致；
- exec 前设置 `DEBATA_DATA_ROOT`，不继承可改写数据根的环境；
- 检查失败或 `setup_required` 未显式 `--allow-setup` 时不 exec。

**没有安装到系统目录、没有改 `debata-agent.service`、没有重启**。
安装/systemd/drop-in 需要当前明确授权；未部署时生产迁移仍 blocked。

在受管部署中，建议流程：

1. 更新/回退计划携带目标完整 SHA 和 manifest hash。
2. apply 前短暂进入维护屏障，停止新迁移/GC/删除领取，并结算当前 item。
3. 重验本地数据快照、manifest、SHA、依赖。
4. 切换源码后由受信 launcher 再检查一次，再启动 Runtime。
5. 不兼容进入 maintenance/blocked，而不是“先运行旧代码试试”。

## 边界

- 本门禁不是数据迁移，也不是备份；D01 目前只有隔离合成库上的迁移模块/
  bootstrap permit，未执行任何生产迁移。
- 源码 ref 备份不恢复聊天/媒体/墓碑；数据恢复必须使用一致备份并重放较新的
  删除 ledger/媒体 manifest。
- 已发送的 QQ 消息、已执行工具动作不能因代码回退而撤销或重放。
- 旧 tag 的兼容映射必须由受信发布侧按完整 SHA 登记；不能只凭 tag 名字放行。
- `compatibility.json` 的 hash 在计划中固定；apply 会重新检查。
