# 设备 Bootloader 锁定/解锁 oem 命令

本文档定位：描述平台 Bootloader 锁定/解锁与 GSI 烧写使用的 fastboot oem 命令选型——`board:<action>` 与 `at-<action>-vboot` 的判定依据、执行路径差异、失败兜底与恢复行为。判定逻辑实现在 `worker_agent/fastboot_workflow.py` 的 `PreparedFastbootDevice.oem_argument()` 与 `FastbootPreparer.apply_oem_action()`。

## 背景（2026-09 锁定事故根因）

commit `641e843` 之前，RK3572 用 `oem board:<action>`、其余平台一律用 `oem at-<action>-vboot`，从未出错；该提交为修复 RK3588 GSI 烧写改为按 `ro.build.version.sdk >= 37` 选 `board:`，并只给 unlock 路径加了 unrecognized 兜底。这引入两层问题：

1. **判据用错**：`ro.build.version.sdk` 是 system 侧（SSI 底座）版本。GRF+SSI 构建（如 RK3562GMS1：A17 SSI 底座 + A14 vendor）的 system 侧同样报 SDK 37，但 uboot 随 **vendor 固件**发布、停留在 A14，只认 `at-lock-vboot`。锁定被误发 `board:lock`。
2. **失败被吞**：lock 路径无 unrecognized 兜底，`scripts/run_Device_Lock.sh` 以 `set -e` 在第一条命令就退出，设备滞留 fastboot（表现为"无法开机"）；同时 API 层只在脚本退出码为 0 时记录结果，失败被静默丢弃，前端把空 `results` 判为全部成功并提示"设备锁定完成"。

## 判定依据（真机 getprop 实测）

| 属性 | RK3588GMS7（纯 A17） | RK3562GMS1（GRF+SSI：A17 底座 + A14 vendor） | 结论 |
|---|---|---|---|
| `ro.build.version.sdk` | 37 | 37 | ✗ 不可用——两台相同，正是误判根源 |
| `ro.product.first_api_level` | 37 | 37 | ✗ 不可用 |
| **`ro.vendor.api_level`** | **202604** | **34** | ✓ **有效判据**：vendor 侧 API level |
| `ro.board.api_level` | 202604 | （缺失） | 佐证，但 GRF+SSI 上缺失 |
| `ro.rksdk.version` | ANDROID17_RKR5 | ANDROID14_RKR9 | 佐证 |
| `ro.boot.fwver`（uboot 段） | `uboot-d680b580b3-09/18/2026` | `uboot-6f7f61e672-09/16/2026` | 仅提供 uboot 构建指纹，hash 无语义 |

机制吻合：oem lock/unlock 由 uboot 实现，uboot 跟随 vendor 固件发布，不受 SSI 系统底座影响——因此必须看 vendor 侧版本。

**反例警示**：vendor 侧属性并非都可靠。纯 A17 的 RK3588GMS7 上 `ro.vendor.sdkversion` 仍是 `rk3588_ANDROID14.0_MID_V1.0`（继承旧值未更新），不能用作判据。选判据必须真机实测。

## oem 命令选择矩阵（现行实现）

| 构建形态 | 判据 | 锁定命令 | 解锁命令 | unrecognized 兜底 |
|---|---|---|---|---|
| RK3572（任意版本） | identity 含 `rk3572` | `oem board:lock` | `oem board:unlock` | 对应 `at-*-vboot` |
| 纯 Android 17（如 RK3588GMS7） | `ro.vendor.api_level >= 202604` | `oem board:lock` | `oem board:unlock` | 对应 `at-*-vboot` |
| GRF+SSI（如 RK3562GMS1）及一切旧 vendor | `ro.vendor.api_level < 202604`（含未知=0） | `oem at-lock-vboot` | `oem at-unlock-vboot` | 对应 `board:*` |

- 常量：`ANDROID_17_VENDOR_API_LEVEL = 202604`（`worker_agent/fastboot_workflow.py`）。
- 版本未知（设备已在 fastboot、无 ADB 读取窗口）按旧 vendor 处理，默认 `at-*-vboot`，由 unrecognized 兜底纠正误判。

## 执行路径对比

| 路径 | 入口 | 解锁命令 | 锁定命令 | unrecognized 兜底 | 后续动作 |
|---|---|---|---|---|---|
| **GSI 烧写**（直连） | GSI 烧写 API → `gsi_transport.prepare_gsi_command` | Python 侧执行（`apply_oem_action`） | **不回锁**，烧完保持解锁 | 有 | `reboot fastboot` → 等 fastbootd → 脚本 flash `system`/`misc`（/vendor），`GMS_GSI_DEFER_REBOOT=1` 时由 Python 控制重启 |
| **设备锁定/解锁**（直连） | `/api/devices/bootloader-lock\|unlock` → `bootloader_api._run_bootloader_lock_block` | 同上 | 同上 | 有 | `run_Device_Lock.sh <serial> -`：`reboot fastboot` → 等 fastbootd → `reboot`，再等 ADB device（60s）；失败时尽力 `fastboot reboot` 拉回系统 |
| **设备锁定/解锁**（集群 Worker） | `/api/cluster/devices/actions` `bootloader_lock/unlock` → `worker_agent/device_actions.py` | 标准 AOSP 命令 `fastboot flashing unlock`（非 oem） | `fastboot flashing lock` | **无** | `adb reboot bootloader` 后直接执行，无平台/版本区分 |

## uboot 支持性判定手段（四层次）

| 层次 | 手段 | 特点 |
|---|---|---|
| 1. 直接执行 | 发 `oem board:lock`，读应答：`OKAY` 认 / `FAILED (remote: 'unrecognized command')` 不认 | 最权威；但 lock 有副作用，只适合事中判定（兜底重试），不能事前探测 |
| 2. 安全探测 | 设备处于解锁态时先发 `oem board:unlock`（幂等 no-op）：`OKAY` → board: 族支持，可发 `board:lock`；unrecognized → 改发 `at-lock-vboot`。同族命令支持性对称 | 零副作用、事前 100% 确定；代价是每次多一个 fastboot round-trip。**决策：暂不采用**，现行兜底重试已等效安全 |
| 3. 间接判据 | ADB 阶段读 `ro.vendor.api_level`（≥ 202604 → board:） | 现行实现的一次性确定性选择；必须真机实测判据（见反例警示） |
| 4. 源码级 | 由 `ro.boot.fwver` 的 uboot hash 对应源码 commit 查 fastboot oem 命令注册表 | 根本判定；需 uboot 源码仓库，运维成本高 |

现行组合 = **3（事前确定性选择）+ 1（事中 unrecognized 兜底）**：能读到 vendor 版本就一次选对；读不到则默认旧命令、失败自动换备选，两条路最终都落到正确命令。

## 失败兜底与恢复

- `apply_oem_action`：仅在应答明确含 `unrecognized` 时以备选命令重试一次；传输类失败按原样抛出（设备可能已接受命令，不盲目重试）。
- `scripts/run_Device_Lock.sh`：oem 参数为 `-` 时跳过 oem 执行（oem 已在 Python 侧完成），只负责 `reboot fastboot` → 等 fastbootd（`FASTBOOTD_TIMEOUT_SECONDS`，默认 60s）→ `reboot`。
- `bootloader_api._run_bootloader_lock_block`：逐设备记录结果（成功/失败均记录）；脚本失败时尽力执行 `fastboot -s <serial> reboot` 把滞留 fastboot 的设备拉回系统；空 `results` 按失败返回，不得判为成功。
