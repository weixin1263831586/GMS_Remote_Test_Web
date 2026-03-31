#!/bin/bash
# ==============================================================================
# GMS Auto Test 服务重启脚本
# ==============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="/home/hcq/GMS_Auto_Test/web_app"
cd "$PROJECT_DIR"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  GMS Auto Test 服务重启脚本${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# ==================== 1. 清理缓存 ====================
echo -e "${YELLOW}[1/4] 清理 Python 缓存...${NC}"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
echo -e "${GREEN}✓ Python 缓存已清理${NC}"
echo ""

# ==================== 2. 停止现有服务 ====================
echo -e "${YELLOW}[2/4] 停止现有服务...${NC}"

for port in 5001 5000; do
    if lsof -i :"$port" >/dev/null 2>&1; then
        echo -e "  停止$port端口服务..."
        fuser -k "$port/tcp" 2>/dev/null || true
        sleep 1
        echo -e "${GREEN}  ✓ $port端口已停止${NC}"
    else
        echo -e "${BLUE}  ℹ $port端口未运行${NC}"
    fi
done
echo ""

# ==================== 3. 清理日志文件 ====================
echo -e "${YELLOW}[3/4] 清理日志文件...${NC}"
[ -f "fastapi.log" ] && mv fastapi.log "fastapi.log.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
[ -f "flask.log" ] && mv flask.log "flask.log.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
echo -e "${GREEN}✓ 日志已清理${NC}"
echo ""

# ==================== 4. 启动服务 ====================
echo -e "${YELLOW}[4/4] 启动服务...${NC}"

# 等待端口可用的函数
wait_for_port() {
    local port=$1
    local max_attempts=${2:-30}
    local attempt=1
    while ! lsof -i :"$port" >/dev/null 2>&1; do
        if [ $attempt -ge $max_attempts ]; then
            return 1
        fi
        sleep 1
        ((attempt++))
    done
    return 0
}

# 启动 FastAPI (5001)
echo -e "  启动 FastAPI (5001)..."
nohup python3 -m uvicorn app_fastapi_full:app \
    --host 0.0.0.0 --port 5001 \
    --log-level info --access-log \
    >> fastapi.log 2>&1 &
sleep 1

if wait_for_port 5001; then
    echo -e "${GREEN}  ✓ FastAPI 启动成功${NC}"
else
    echo -e "${RED}  ✗ FastAPI 启动失败：tail -f fastapi.log${NC}"
    exit 1
fi

# 启动 Flask (5000)
echo -e "  启动 Flask (5000)..."
nohup python3 app.py >> flask.log 2>&1 &
sleep 1

if wait_for_port 5000; then
    echo -e "${GREEN}  ✓ Flask 启动成功${NC}"
else
    echo -e "${RED}  ✗ Flask 启动失败：tail -f flask.log${NC}"
    exit 1
fi
echo ""

# ==================== 完成 ====================
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}  ✓ 所有服务启动成功！${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "FastAPI: ${GREEN}http://localhost:5001${NC}"
echo -e "Flask:  ${GREEN}http://localhost:5000${NC}"
echo ""
echo -e "日志：${BLUE}tail -f fastapi.log | tail -f flask.log${NC}"
echo -e "停止：${BLUE}fuser -k 5001/tcp && fuser -k 5000/tcp${NC}"
