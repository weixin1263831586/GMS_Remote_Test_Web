#!/bin/bash
# ==============================================================================
# GMS Auto Test 服务管理脚本
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="/home/hcq/GMS_Auto_Test/web_app"
cd "$PROJECT_DIR"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  GMS Auto Test 服务管理${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 1. 清理缓存
echo -e "${YELLOW}[1/4] 清理 Python 缓存...${NC}"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
echo -e "${GREEN}✓ 缓存已清理${NC}"
echo ""

# 2. 备份旧日志
echo -e "${YELLOW}[2/4] 备份日志...${NC}"
for log in fastapi.log flask.log; do
    [[ -f "$log" ]] && mv "$log" "${log}.backup.$(date +%Y%m%d_%H%M%S)"
done
echo -e "${GREEN}✓ 日志已备份${NC}"
echo ""

# 3. 停止旧服务
echo -e "${YELLOW}[3/4] 停止旧服务...${NC}"
for port in 5001 5000; do
    if lsof -i :"$port" >/dev/null 2>&1; then
        fuser -k "$port/tcp" 2>/dev/null || true
        sleep 1
        echo -e "${GREEN}  ✓ ${port} 已停止${NC}"
    else
        echo -e "${BLUE}  ℹ ${port} 未运行${NC}"
    fi
done
echo ""

# 4. 启动新服务
echo -e "${YELLOW}[4/4] 启动新服务...${NC}"

echo -e "  启动 FastAPI (5001)..."
nohup python3 -m uvicorn app_fastapi_full:app \
    --host 0.0.0.0 --port 5001 --log-level info --access-log \
    >> fastapi.log 2>&1 &
sleep 2

echo -e "  启动 Flask (5000)..."
nohup python3 app.py >> flask.log 2>&1 &
sleep 2

# 健康检查
for port in 5001 5000; do
    if curl -s -f "http://localhost:${port}/" >/dev/null 2>&1; then
        echo -e "${GREEN}  ✓ ${port} 启动成功${NC}"
    else
        echo -e "${YELLOW}  ⚠ ${port} 健康检查失败${NC}"
    fi
done
echo ""

echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}  ✓ 服务管理完成！${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "服务地址："
echo -e "  FastAPI: ${GREEN}http://localhost:5001${NC}"
echo -e "  Flask:   ${GREEN}http://localhost:5000${NC}"
echo ""
