# 路由命令终端静默模式优化

## 问题描述

之前的实现中，跳转到主机终端时会先显示连接信息，然后清空再显示路由命令提示，用户体验不佳。

```
hcq@ats-041055-64g:~$ ========================================
📋 路由命令已准备就绪
========================================

命令: sudo ip route add 172.16.21.0/24 via 172.16.14.1

提示: 按 Enter 执行命令，可能需要输入 sudo 密码

========================================

sudo ip route add 172.16.21.0/24 via 172.16.14.1
hcq@ats-041055-64g:~$
```

## 解决方案

参考ADB shell的处理方式，实现**静默模式**：
1. 在后台缓冲所有终端输出
2. 检测到shell提示符后（如 `hcq@ats-041055-64g:~$`）
3. 清空缓冲区，只显示路由命令提示信息
4. 自动输入命令，等待用户按回车

## 技术实现

### 1. 添加路由命令模式变量 (templates/index_fastapi.html)

```javascript
// 路由命令模式下的静默处理
let routeCommandMode = false;  // 是否在路由命令模式
let pendingRouteCommand = null;  // 待执行的路由命令
let routeCommandBuffer = [];  // 路由命令输出缓冲区
```

### 2. 页面切换时设置路由命令模式

```javascript
if (pageName === 'terminal') {
    const pendingCommand = sessionStorage.getItem('pending_terminal_command');
    const commandSource = sessionStorage.getItem('command_source');

    // 如果有路由命令，保存到全局变量
    if (pendingCommand && commandSource === 'route_check') {
        pendingRouteCommand = pendingCommand;
        routeCommandMode = true;
        routeCommandBuffer = [];
        sessionStorage.removeItem('pending_terminal_command');
        sessionStorage.removeItem('command_source');
        console.log('Route command mode enabled, command:', pendingRouteCommand);
    }

    initTerminal();
}
```

### 3. WebSocket连接时启用静默模式

```javascript
terminalSocket.onopen = () => {
    console.log('Terminal WebSocket connected');
    updateTerminalStatus(true);

    // 检查是否为路由命令模式
    if (routeCommandMode && pendingRouteCommand) {
        console.log('Route command mode: enabling silent mode');
        // 启用静默模式，等待检测到shell提示符
        adbSilentMode = true;
        adbBuffer = [];
        // 请求SSH连接
        terminalSocket.send(JSON.stringify({
            type: 'terminal_connect',
            mode: 'ssh',
            host: terminalConfig.ssh_host,
            user: terminalConfig.ssh_user,
            password: terminalConfig.ssh_password
        }));
        return;
    }

    // ... 其他模式处理
};
```

### 4. 检测shell提示符并显示路由命令

```javascript
if (adbSilentMode) {
    adbBuffer.push(msg.data);
    const text = adbBuffer.join('');

    // 路由命令模式:检测Linux shell提示符
    if (routeCommandMode && pendingRouteCommand) {
        // 匹配常见的Linux shell提示符模式
        // 例如: hcq@ats-041055-64g:~$, root@server:~#, user@host:~$ 等
        if (/^[\w-]+@[\w-]+:.+[$#]\s*$/.test(text) ||
            /@.+[$#]\s*$/.test(text) ||
            text.includes(':~$') || text.includes(':~#')) {

            console.log('Shell prompt detected for route command, flushing buffer');
            adbSilentMode = false;
            routeCommandMode = false;

            // 清空终端
            terminal.clear();

            // 显示路由命令提示信息
            terminal.writeln('\x1b[33m========================================\x1b[0m');
            terminal.writeln('\x1b[33m📋 路由命令已准备就绪\x1b[0m');
            terminal.writeln('\x1b[33m========================================\x1b[0m\r\n');
            terminal.writeln('\x1b[36m命令: ' + pendingRouteCommand + '\x1b[0m\r\n');
            terminal.writeln('\x1b[90m提示: 按 Enter 执行命令，可能需要输入 sudo 密码\x1b[0m\r\n');
            terminal.writeln('\x1b[33m========================================\x1b[0m\r\n');

            // 显示shell提示符
            const lines = text.split('\r\n');
            const lastLine = lines[lines.length - 1];
            terminal.writeln(lastLine);

            // 自动输入命令（但不执行）
            terminal.write(pendingRouteCommand);

            // 聚焦终端
            terminal.focus();

            // 清除路由命令标志
            pendingRouteCommand = null;
        }
    }
    // ... ADB shell检测逻辑
}
```

## 用户体验优化

### 之前的问题
- ❌ 先显示连接信息
- ❌ 然后清空屏幕
- ❌ 再显示路由命令提示
- ❌ 给人"闪烁"的感觉

### 优化后的效果
- ✅ 后台静默连接，不显示任何输出
- ✅ 检测到shell提示符后立即清空
- ✅ 只显示路由命令提示信息
- ✅ 平滑过渡，无闪烁

## Shell提示符匹配模式

支持多种Linux shell提示符格式：

1. **标准格式**: `user@hostname:~$`
2. **root用户**: `root@hostname:~#`
3. **路径变体**: `user@hostname:/path/to/dir$`
4. **简化格式**: `user@host:~$`

正则表达式：
```javascript
/^[\w-]+@[\w-]+:.+[$#]\s*$/.test(text) ||
/@.+[$#]\s*$/.test(text) ||
text.includes(':~$') || text.includes(':~#')
```

## 数据流程

```
用户点击"打开主机终端"
    ↓
保存命令到 pendingRouteCommand
设置 routeCommandMode = true
    ↓
初始化终端连接
    ↓
启用 adbSilentMode (静默模式)
    ↓
缓冲所有输出到 adbBuffer
    ↓
检测 shell 提示符 (hcq@ats-041055-64g:~$)
    ↓
清空终端屏幕
    ↓
显示路由命令提示信息
    ↓
自动输入命令
    ↓
等待用户按回车
```

## 错误处理

```javascript
} else if (msg.type === 'terminal_error') {
    console.error('Terminal error:', msg.error);
    if (terminal) {
        terminal.writeln(`\r\n\x1b[31m❌ 错误: ${msg.error}\x1b[0m\r\n`);
    }
    updateTerminalStatus(false);
    adbSilentMode = false;  // 出错时退出静默模式
    routeCommandMode = false;  // 出错时退出路由命令模式
}
```

## 测试步骤

1. 启动应用：`python3 app_fastapi_full.py`
2. 打开浏览器访问：`http://localhost:5001`
3. 点击"📡 检查路由连通性"
4. 输入不同网段的IP地址
5. 点击"🔍 测试连通性"
6. 点击"🖥️ 打开主机终端"
7. 验证：
   - ✅ 终端平滑显示，无闪烁
   - ✅ 直接显示路由命令提示
   - ✅ 命令已自动输入
   - ✅ 按回车可以执行

## 相关文件

- `templates/index_fastapi.html` - 终端静默模式实现
- `static/js/app.js` - 路由检查和终端按钮逻辑
- `static/css/style.css` - 弹框和按钮样式

## 版本历史

- **2026-04-05 v1** - 初始版本，直接显示命令（有闪烁）
- **2026-04-05 v2** - 优化版本，使用静默模式（无闪烁，参考ADB shell实现）
