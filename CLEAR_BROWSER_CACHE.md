# 解决 "checkRouting is not defined" 错误

## 问题原因

浏览器缓存了旧版本的 `app.js` 文件，该版本中没有 `checkRouting` 函数的新定义。

## 已修复的问题

✅ **JavaScript语法错误**: 修复了Python切片语法 `[:3]` 为JavaScript语法 `.slice(0,3)`
✅ **HTML版本号**: 更新了 `app.js` 的版本号为 `v=20260331-19`
✅ **函数导出**: 确认 `window.checkRouting = checkRouting` 已正确添加

## 解决方案

### 方案1: 强制刷新浏览器（推荐）

**Windows/Linux:**
- 按 `Ctrl + F5`
- 或按 `Ctrl + Shift + R`

**Mac:**
- 按 `Cmd + Shift + R`

### 方案2: 清除浏览器缓存

**Chrome:**
1. 按 `F12` 打开开发者工具
2. 右键点击浏览器的刷新按钮
3. 选择"清空缓存并硬性重新加载"

**或者:**
1. 按 `F12` 打开开发者工具
2. 进入 "Network"（网络）标签
3. 勾选 "Disable cache"（禁用缓存）
4. 刷新页面 (`F5`)

### 方案3: 使用无痕模式测试

1. 打开无痕/隐私浏览窗口
2. 访问 `http://172.16.14.233:5001`
3. 测试功能

### 方案4: 手动清除特定缓存

1. 按 `F12` 打开开发者工具
2. 进入 "Application"（应用）标签
3. 左侧找到 "Storage" → "Clear site data"
4. 点击 "Clear site data" 按钮

## 验证步骤

### 1. 确认新版本已加载

打开浏览器控制台（按 `F12`），在Console中输入：

```javascript
typeof checkRouting
```

应该返回 `"function"`，而不是 `"undefined"`

### 2. 检查加载的文件版本

在Network标签中，找到 `app.js?v=20260331-19`，确认：
- 状态码是 `200`（不是 `304`）
- 大小约为 `257KB`（包含新功能）

### 3. 测试功能

1. 点击"📡 检查路由"按钮
2. 应该打开一个对话框
3. 输入IP地址并测试

## 服务器端验证

如果上述方法都不行，从服务器端确认文件已更新：

```bash
# 检查文件修改时间
ls -lh /home/hcq/GMS_Auto_Test/web_app/static/js/app.js

# 检查函数是否存在
grep -n "async function checkRouting" /home/hcq/GMS_Auto_Test/web_app/static/js/app.js

# 检查语法
node --check /home/hcq/GMS_Auto_Test/web_app/static/js/app.js
```

## 临时解决方案

如果急需使用，可以直接在浏览器控制台中执行：

```javascript
# 打开控制台（F12），粘贴以下代码：
checkRouting();
```

这应该会直接打开路由检查对话框。

## 预防措施

为避免将来的缓存问题：

1. **开发时**: 在开发者工具中勾选 "Disable cache"
2. **部署时**: 使用文件哈希作为版本号而不是日期
3. **测试时**: 使用无痕模式进行测试

## 当前状态

✅ JavaScript语法已修复
✅ HTML版本号已更新
✅ 函数已正确导出到window对象
✅ 服务器正在运行最新代码

**只需要清除浏览器缓存即可！**

## 仍然不工作？

如果尝试了所有方案仍然不工作，请：

1. 确认访问的URL是: `http://172.16.14.233:5001`
2. 检查浏览器控制台是否有其他错误
3. 尝试使用不同的浏览器
4. 重启浏览器

## 联系支持

如果问题持续，请提供：
- 浏览器类型和版本
- 浏览器控制台的完整错误信息
- Network标签中 `app.js` 的加载状态
