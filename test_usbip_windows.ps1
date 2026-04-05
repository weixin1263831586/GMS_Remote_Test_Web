# Windows PowerShell USB/IP测试脚本
# 使用UTF-8编码确保中文正常显示

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

# 设置API基础URL
$BASE_URL = "http://172.16.14.233:5001"

Write-Host "🔧 USB/IP API 测试" -ForegroundColor Cyan
Write-Host "==================" -ForegroundColor Cyan
Write-Host ""

# 测试1: 获取USB/IP状态
Write-Host "1️⃣  获取USB/IP状态" -ForegroundColor Yellow
Write-Host "GET $BASE_URL/api/usbip/status"
$statusResponse = curl.exe -s "$BASE_URL/api/usbip/status" | jq "."
Write-Host $statusResponse
Write-Host ""

# 测试2: 启动USB/IP
Write-Host "2️⃣  启动USB/IP" -ForegroundColor Yellow
Write-Host "POST $BASE_URL/api/usbip/start"
Write-Host "请求体: { 'device_host': '172.16.14.68', 'device_password': 'VALUE' }"

# 使用Here-String确保JSON格式正确
$startResponse = curl.exe -sX POST "$BASE_URL/api/usbip/start" `
    -H "Content-Type: application/json" `
    -d @{
        device_host = "172.16.14.68"
        device_password = "VALUE"
    } | jq "."

Write-Host $startResponse
Write-Host ""

# 测试3: 停止USB/IP
Write-Host "3️⃣  停止USB/IP" -ForegroundColor Yellow
Write-Host "POST $BASE_URL/api/usbip/stop"
$stopResponse = curl.exe -sX POST "$BASE_URL/api/usbip/stop" | jq "."
Write-Host $stopResponse
Write-Host ""

Write-Host "✅ 测试完成" -ForegroundColor Green
Write-Host ""
Write-Host "💡 提示：" -ForegroundColor Cyan
Write-Host "  - 如果看到乱码，请在PowerShell中运行: [Console]::OutputEncoding = [System.Text.Encoding]::UTF8"
Write-Host "  - 或者使用Git Bash或WSL运行curl命令"
