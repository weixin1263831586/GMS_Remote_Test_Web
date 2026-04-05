# Windows PowerShell USB/IP API 测试指南

## 🔧 问题解决

### 问题1: JSON格式错误

**❌ 错误的命令**:
```powershell
curl.exe -sX POST "http://172.16.14.233:5001/api/usbip/start" -H "Content-Type: application/json" -d '{"device_host": "172.16.14.68", "device_password": "VALUE"}'
```

**✅ 正确的命令**:

#### 方法1: 转义双引号（推荐）
```powershell
curl.exe -sX POST "http://172.16.14.233:5001/api/usbip/start" -H "Content-Type: application/json" -d '{\"device_host\": \"172.16.14.68\", \"device_password\": \"VALUE\"}' | jq "."
```

#### 方法2: 使用Here-String（最安全）
```powershell
curl.exe -sX POST "http://172.16.14.233:5001/api/usbip/start" -H "Content-Type: application/json" -d @'
{
  "device_host": "172.16.14.68",
  "device_password": "VALUE"
}
'@ | jq "."
```

#### 方法3: 使用ConvertTo-Json（PowerShell原生方式）
```powershell
$body = @{
    device_host = "172.16.14.68"
    device_password = "VALUE"
} | ConvertTo-Json

curl.exe -sX POST "http://172.16.14.233:5001/api/usbip/start" -H "Content-Type: application/json" -d $body | jq "."
```

---

### 问题2: 中文乱码

#### 解决方法A: 设置PowerShell编码
```powershell
# 在PowerShell开始前运行
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

# 然后再运行curl命令
curl.exe -sX POST "http://172.16.14.233:5001/api/usbip/stop" | jq "."
```

#### 解决方法B: 使用Git Bash或WSL
```bash
# 在Git Bash或WSL中运行（推荐）
curl -sX POST "http://172.16.14.233:5001/api/usbip/stop" | jq "."
```

---

## 📋 完整测试命令

### 1. 获取USB/IP状态
```powershell
# 设置编码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 执行命令
curl.exe -s "http://172.16.14.233:5001/api/usbip/status" | jq "."
```

### 2. 启动USB/IP
```powershell
# 方法1: 转义引号
curl.exe -sX POST "http://172.16.14.233:5001/api/usbip/start" -H "Content-Type: application/json" -d '{\"device_host\": \"172.16.14.68\", \"device_password\": \"VALUE\"}' | jq "."

# 方法2: PowerShell对象
$body = @{
    device_host = "172.16.14.68"
    device_password = "VALUE"
} | ConvertTo-Json

curl.exe -sX POST "http://172.16.14.233:5001/api/usbip/start" -H "Content-Type: application/json" -d $body | jq "."
```

### 3. 停止USB/IP
```powershell
curl.exe -sX POST "http://172.16.14.233:5001/api/usbip/stop" | jq "."
```

---

## 🎯 快速测试脚本

创建文件 `test_usbip.ps1`:

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$BASE_URL = "http://172.16.14.233:5001"

Write-Host "🔧 USB/IP API 测试" -ForegroundColor Cyan

# 测试状态
Write-Host "1️⃣  获取状态" -ForegroundColor Yellow
curl.exe -s "$BASE_URL/api/usbip/status" | jq "."

# 测试停止
Write-Host "2️⃣  停止USB/IP" -ForegroundColor Yellow
curl.exe -sX POST "$BASE_URL/api/usbip/stop" | jq "."
```

运行：
```powershell
.\test_usbip.ps1
```

---

## 💡 最佳实践

### ✅ 推荐: 使用Git Bash

1. 安装 [Git for Windows](https://git-scm.com/download/win)
2. 打开 Git Bash
3. 运行标准Linux命令：

```bash
# 获取状态
curl -s "http://172.16.14.233:5001/api/usbip/status" | jq "."

# 启动USB/IP
curl -sX POST "http://172.16.14.233:5001/api/usbip/start" \
  -H "Content-Type: application/json" \
  -d '{
    "device_host": "172.16.14.68",
    "device_password": "VALUE"
  }' | jq "."

# 停止USB/IP
curl -sX POST "http://172.16.14.233:5001/api/usbip/stop" | jq "."
```

### ✅ 推荐: 使用WSL (Windows Subsystem for Linux)

```bash
# 在WSL中运行与Git Bash相同的命令
curl -s "http://172.16.14.233:5001/api/usbip/status" | jq "."
```

---

## 🐛 调试提示

### 查看原始响应（不含jq）
```powershell
curl.exe -s "http://172.16.14.233:5001/api/usbip/stop"
```

### 查看HTTP头
```powershell
curl.exe -v "http://172.16.14.233:5001/api/usbip/stop" 2>&1 | Select-String "Content-Type"
```

### 检查编码
```powershell
curl.exe -s "http://172.16.14.233:5001/api/usbip/stop" | jq ".message" | Format-Hex
```

---

## 📚 总结

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| JSON格式错误 | PowerShell双引号处理 | 使用转义或Here-String |
| 中文乱码 | PowerShell默认编码 | 设置UTF-8编码或使用Git Bash |

**最简单的解决方案**: 使用Git Bash或WSL运行标准的Linux curl命令！
