# 路由检查终端按钮功能

## 功能描述

在"📡 检查路由连通性"弹框中，当路由测试失败时，会显示"🖥️ 打开主机终端"按钮。点击该按钮后：

1. **自动关闭路由检查弹框**
2. **自动跳转到主机终端页面**
3. **自动输入路由命令**到终端，等待用户按回车执行

## 使用流程

### 1. 触发路由检查
点击"📡 检查路由连通性"按钮

### 2. 测试失败场景
当测试主机和客户端不在同一网段且无法连通时，系统会：
- 显示路由失败信息
- 显示建议的路由命令（Linux和Windows）
- 显示"🖥️ 打开主机终端"按钮

### 3. 点击终端按钮
点击"🖥️ 打开主机终端"按钮后：

#### 前端操作 (static/js/app.js)
```javascript
// 保存命令到 sessionStorage
sessionStorage.setItem('pending_terminal_command', command);
sessionStorage.setItem('command_source', 'route_check');

// 关闭路由检查弹框
document.body.removeChild(dialog);

// 切换到终端页面
switchPage('terminal');
```

#### 终端页面处理 (templates/index_fastapi.html)
```javascript
// 检查是否有待执行的命令
const pendingCommand = sessionStorage.getItem('pending_terminal_command');

// 等待终端连接完成后（2秒）
setTimeout(() => {
    // 清空终端并显示提示
    terminal.clear();
    terminal.writeln('📋 路由命令已准备就绪');
    terminal.writeln('命令: ' + pendingCommand);
    terminal.writeln('提示: 按 Enter 执行命令，可能需要输入 sudo 密码');

    // 自动输入命令（但不执行）
    terminal.write(pendingCommand);

    // 聚焦终端
    terminal.focus();
}, 2000);
```

### 4. 执行命令
用户看到：
```
========================================
📋 路由命令已准备就绪
========================================

命令: sudo ip route add 172.16.21.0/24 via 172.16.14.1

提示: 按 Enter 执行命令，可能需要输入 sudo 密码
========================================

sudo ip route add 172.16.21.0/24 via 172.16.14.1_
```

用户只需：
1. **按回车键** - 执行命令
2. **输入sudo密码**（如果需要）
3. **按回车确认** - 完成路由添加

## 技术实现

### 关键文件

1. **static/js/app.js** (行 1743-1791)
   - 路由检查弹框的终端按钮事件处理
   - 保存命令到 sessionStorage
   - 调用 switchPage 切换页面

2. **templates/index_fastapi.html** (行 2073-2214)
   - switchPage 函数处理页面切换
   - 检查 sessionStorage 中的待执行命令
   - 在终端连接后自动输入命令

3. **app_fastapi_full.py** (行 5181-5278)
   - `/api/ssh/terminal/open` API端点
   - 命令安全性验证
   - 返回操作说明

### 数据流程

```
路由检查弹框
    ↓ (用户点击"打开主机终端")
sessionStorage: pending_terminal_command
    ↓ (switchPage('terminal'))
终端页面初始化
    ↓ (等待2秒连接)
自动输入命令到终端
    ↓ (等待用户按回车)
用户执行命令
```

### 样式更新 (static/css/style.css)

- 固定标题栏和关闭按钮（滚动时保持可见）
- 终端按钮样式（蓝色主题，悬停效果）
- 成功/错误消息样式

## 安全特性

### 命令验证 (app_fastapi_full.py)
```python
def _is_safe_route_command(command: str) -> bool:
    """验证路由命令是否安全"""
    safe_prefixes = [
        'sudo ip route add',
        'sudo ip route del',
        'route add',
        'route delete',
        'ip route add',
        'ip route del'
    ]

    # 检查命令前缀
    # 检查危险字符（|, &, ;, $, `, >, <, \n, \r）
```

### 防护措施
- ✅ 只允许特定的路由命令
- ✅ 阻止命令注入攻击
- ✅ 不自动执行，等待用户确认
- ✅ 清除 sessionStorage 防止重复执行

## 用户体验优化

### 1. 一键操作
- 无需手动复制命令
- 无需手动切换页面
- 无需手动粘贴命令

### 2. 清晰提示
- 终端显示醒目的提示信息
- 明确告知需要按回车执行
- 提醒可能需要输入密码

### 3. 固定标题栏
- 路由检查弹框滚动时，标题和关闭按钮始终可见
- 使用 CSS `position: sticky` 实现

### 4. 错误处理
- 如果终端未连接，显示错误消息
- 提供手动操作指南作为备用方案

## 测试步骤

1. 启动应用：`python3 app_fastapi_full.py`
2. 打开浏览器访问：`http://localhost:5001`
3. 点击"📡 检查路由连通性"
4. 输入不同网段的IP地址（如测试主机: 172.16.14.233, 客户端: 192.168.1.100）
5. 点击"🔍 测试连通性"
6. 看到失败信息后，点击"🖥️ 打开主机终端"
7. 验证：
   - ✅ 弹框关闭
   - ✅ 跳转到终端页面
   - ✅ 终端自动输入命令
   - ✅ 按回车可以执行命令

## 注意事项

1. **终端连接时间**：等待2秒是为了确保终端WebSocket完全连接
2. **命令执行**：命令已输入但未执行，用户必须按回车确认
3. **sudo权限**：路由命令需要sudo权限，用户需要输入密码
4. **一次性使用**：命令执行后会从sessionStorage清除，防止重复执行

## 相关文件

- `static/js/app.js` - 路由检查和终端按钮逻辑
- `static/css/style.css` - 弹框和按钮样式
- `templates/index_fastapi.html` - 页面切换和终端初始化
- `app_fastapi_full.py` - 后端API和安全验证

## 版本历史

- **2026-04-05** - 初始版本，实现一键打开终端并自动输入命令功能
