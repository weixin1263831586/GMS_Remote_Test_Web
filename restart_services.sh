#!/bin/bash
# ==============================================================================
# GMS Auto Test 服务重启脚本
# 功能：清理缓存，重启5000端口(Flask)和5001端口(FastAPI)服务
# ==============================================================================

set -e  # 遇到错误立即退出

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目路径
PROJECT_DIR="/home/hcq/GMS_Auto_Test/web_app"
cd "$PROJECT_DIR"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  GMS Auto Test 服务重启脚本${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# ==================== 第1步：清理缓存 ====================
echo -e "${YELLOW}[1/5] 清理Python缓存...${NC}"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
find . -type f -name "*.pyo" -delete 2>/dev/null || true
echo -e "${GREEN}✓ Python缓存已清理${NC}"
echo ""

# ==================== 第2步：停止现有服务 ====================
echo -e "${YELLOW}[2/5] 停止现有服务...${NC}"

# 停止5001端口服务 (FastAPI)
if lsof -i :5001 >/dev/null 2>&1; then
    echo -e "  停止5001端口服务..."
    fuser -k 5001/tcp 2>/dev/null || true
    sleep 1
    echo -e "${GREEN}  ✓ 5001端口已停止${NC}"
else
    echo -e "${BLUE}  ℹ 5001端口未运行${NC}"
fi

# 停止5000端口服务 (Flask)
if lsof -i :5000 >/dev/null 2>&1; then
    echo -e "  停止5000端口服务..."
    fuser -k 5000/tcp 2>/dev/null || true
    sleep 1
    echo -e "${GREEN}  ✓ 5000端口已停止${NC}"
else
    echo -e "${BLUE}  ℹ 5000端口未运行${NC}"
fi

echo ""

# ==================== 第3步：清理日志文件(可选) ====================
echo -e "${YELLOW}[3/5] 清理日志文件...${NC}"
if [ -f "fastapi.log" ]; then
    mv fastapi.log "fastapi.log.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
    echo -e "${GREEN}  ✓ FastAPI日志已备份${NC}"
fi
if [ -f "flask.log" ]; then
    mv flask.log "flask.log.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
    echo -e "${GREEN}  ✓ Flask日志已备份${NC}"
fi
echo ""

# ==================== 第4步：启动FastAPI服务(5001端口) ====================
echo -e "${YELLOW}[4/5] 启动FastAPI服务(5001端口)...${NC}"
nohup python3 -m uvicorn app_fastapi_full:app \
    --host 0.0.0.0 \
    --port 5001 \
    --log-level info \
    --access-log \
    >> fastapi.log 2>&1 &

FASTAPI_PID=$!
sleep 2

# 检查FastAPI是否启动成功
if ps -p $FASTAPI_PID > /dev/null; then
    echo -e "${GREEN}  ✓ FastAPI服务启动成功 (PID: $FASTAPI_PID)${NC}"
else
    echo -e "${RED}  ✗ FastAPI服务启动失败${NC}"
    echo -e "${RED}  请检查日志: tail -f fastapi.log${NC}"
    exit 1
fi
echo ""

# ==================== 第5步：启动Flask服务(5000端口) ====================
echo -e "${YELLOW}[5/5] 启动Flask服务(5000端口)...${NC}"
nohup python3 app.py \
    >> flask.log 2>&1 &

FLASK_PID=$!
sleep 2

# 检查Flask是否启动成功
if ps -p $FLASK_PID > /dev/null; then
    echo -e "${GREEN}  ✓ Flask服务启动成功 (PID: $FLASK_PID)${NC}"
else
    echo -e "${RED}  ✗ Flask服务启动失败${NC}"
    echo -e "${RED}  请检查日志: tail -f flask.log${NC}"
    exit 1
fi
echo ""

# ==================== 完成 ====================
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}  ✓ 所有服务启动成功！${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "FastAPI服务: ${GREEN}http://localhost:5001${NC}"
echo -e "Flask服务:  ${GREEN}http://localhost:5000${NC}"
echo ""
echo -e "查看日志:"
echo -e "  FastAPI: ${BLUE}tail -f fastapi.log${NC}"
echo -e "  Flask:  ${BLUE}tail -f flask.log${NC}"
echo ""
echo -e "停止服务:"
echo -e "  ${BLUE}fuser -k 5001/tcp  # FastAPI${NC}"
echo -e "  ${BLUE}fuser -k 5000/tcp  # Flask${NC}"
echo ""
