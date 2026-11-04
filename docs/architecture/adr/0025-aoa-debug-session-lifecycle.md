# ADR 0025：AOA 单条调试会话与回放生命周期

- Status: Accepted
- Date: 2026-11-04
- Related: [ADR 0024](0024-aoa-pointer-compatibility-and-input-modes.md)

## Context

单条调试每次恢复原生 USB，整段回放则一直保留 HID。两者耗时不同，且 Android
Settings 根据鼠标存在与否显示 Mouse 条目，使按两种方式验证的固定坐标发生偏差。

## Decision

1. 提供显式的“单条调试保持 HID 连接”。首次按完整编辑列表冻结通道集合，之后
   只执行用户选中的命令，不在每条完成后移除 HID。保持原设备和物理端口租约，
   等待用户输入期间继续校验 fencing。会话只开放给人类用户和本机 Worker。
2. 使用同一持久 device_action 回执，队列保存到该回执的 payload，支持 Web 多进程。
   请求 ID 绑定命令文本、账号和目标，SQLite BEGIN IMMEDIATE 串行入队和空闲关闭；
   原进程中断后的 running 回执不得重新派发。网络异常不自动重放输入。
3. 原有取消控制用于“结束调试”；空闲 120 秒、总计 900 秒为清理边界。清理完毕
   再结束回执和设备占用。强制释放仍走原管理员、租约 generation 和取消边界。
4. metadata.hid_session_mode 默认为 continuous；可显式选择 per_command 匹配原有
   短会话调试的 USB 生命周期。该模式保留同一设备锁，但每个输入后先完整清理，
   sleep 不重新注册 HID。任何未确认输入或清理失败立即停止，不重新发送。
5. 远端 per_command 需要 Worker 声明 aoa_per_command_v1；持久单条调试尚无远端
   队列协议，因此远端请求 fail closed，不能静默降级。

## Consequences

持久单条调试更接近连续回放的 Mouse/键盘环境，后续单条省去 USB 协商耗时；首次
仍需协商与稳定等待。鼠标是否显示及设置菜单布局由 ROM 决定；两种生命周期都
不保证菜单坐标相同，也不保证任意固定 sleep 足以应对界面动画，仍需真机校验。
离开页面的清理属于用户动作；模块初始化不发送变更请求。关闭浏览器或丢失网络
依靠有界空闲超时，不依靠不可靠的 unload 请求。
