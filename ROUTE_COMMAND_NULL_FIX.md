# 修复路由命令显示为null的问题

## 问题描述

路由命令自动执行时，命令显示为 `null`，导致执行失败：

```
hcq@ats-041055-64g:~$ null
Command 'null' not found, but can be installed with:
sudo snap install null
```

## 问题原因

**错误的代码逻辑**：

```javascript
// 显示命令
terminal.writeln('命令: ' + pendingRouteCommand + '\x1b[0m\r\n');

// 通过WebSocket发送命令
setTimeout(() => {
    const fullCommand = pendingRouteCommand + '\r';  // ❌ 此时pendingRouteCommand已经是null
    terminalSocket.send({ input: fullCommand });
}, 500);

// 清空路由命令标志
pendingRouteCommand = null;  // ❌ 在setTimeout之前就清空了
```

**执行顺序**：
1. 显示命令（此时 `pendingRouteCommand` 还有值）
2. 立即清空 `pendingRouteCommand = null`
3. 500ms后setTimeout回调执行
4. 此时 `pendingRouteCommand` 已经是 `null`
5. 发送的命令变成 `null\r`

## 解决方案

将命令保存到局部变量，避免被提前清空：

```javascript
// 保存命令到局部变量（避免在setTimeout中被清空）
const commandToSend = pendingRouteCommand;

// 清空终端
terminal.clear();

// 显示路由命令提示信息
terminal.writeln('\x1b[36m命令: ' + commandToSend + '\x1b[0m\r\n');

// 通过WebSocket发送命令到服务器
setTimeout(() => {
    if (terminalSocket && terminalSocket.readyState === WebSocket.OPEN && commandToSend) {
        // 发送命令（包括回车）
        const fullCommand = commandToSend + '\r';
        terminalSocket.send(JSON.stringify({
            type: 'terminal_input',
            input: fullCommand
        }));
        console.log('Route command sent:', fullCommand);
    }
}, 500);

// 聚焦终端
terminal.focus();

// 清除路由命令标志（在保存命令之后）
pendingRouteCommand = null;
```

## 关键改进

### 1. 使用局部变量保存命令
```javascript
const commandToSend = pendingRouteCommand;
```

### 2. 使用局部变量显示和发送
```javascript
terminal.writeln('命令: ' + commandToSend);
const fullCommand = commandToSend + '\r';
```

### 3. 添加空值检查
```javascript
if (terminalSocket && terminalSocket.readyState === WebSocket.OPEN && commandToSend) {
    // 发送命令
}
```

### 4. 添加调试日志
```javascript
console.log('Route command sent:', fullCommand);
```

## 执行流程

```
1. 检测到shell提示符
   ↓
2. 保存命令到局部变量: commandToSend = pendingRouteCommand
   ↓
3. 清空终端屏幕
   ↓
4. 显示路由命令提示（使用commandToSend）
   ↓
5. 设置500ms延迟
   ↓
6. 立即清空pendingRouteCommand = null
   ↓
7. 500ms后setTimeout回调执行
   ↓
8. 检查commandToSend（不为null）
   ↓
9. 发送命令到服务器: commandToSend + '\r'
   ↓
10. 命令执行成功！✅
```

## 对比

| 项目 | 错误实现 | 正确实现 |
|------|---------|---------|
| 变量使用 | 直接使用pendingRouteCommand | 保存到局部变量commandToSend |
| 清空时机 | setTimeout之前 | setTimeout之后（但用局部变量） |
| 空值检查 | 无 | 有 |
| 调试日志 | 无 | 有 |
| 执行结果 | 命令为null | 命令正常执行 |

## 测试验证

### 预期结果

```
========================================
📋 路由命令已准备就绪
========================================

命令: sudo ip route add 10.10.10.0/24 via 172.16.14.1

提示: 正在自动执行命令，可能需要输入 sudo 密码

========================================

hcq@ats-041055-64g:~$ sudo ip route add 10.10.10.0/24 via 172.16.14.1
[sudo] hcq 的密码：
```

### 控制台日志

```
Shell prompt detected for route command, flushing buffer
Route command sent: sudo ip route add 10.10.10.0/24 via 172.16.14.1
```

## 版本历史

- **2026-04-05 v1** - 初始实现，命令显示但不自动执行
- **2026-04-05 v2** - 添加自动执行，但变量作用域问题导致命令为null
- **2026-04-05 v3** - 使用局部变量保存命令，修复null问题
