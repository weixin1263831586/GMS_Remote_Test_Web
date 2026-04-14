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

# 帮助信息
show_help() {
    cat << EOF
用法: $0 [选项] [端口]

选项:
  -h, --help          显示此帮助信息
  -c, --clean-logs    清理旧的日志备份文件（保留最近5个）
  -f, --fast          快速模式，跳过缓存清理
  PORT                指定端口重启（5001 或 5000），不指定则重启所有

示例:
  $0                  重启所有服务
  $0 5001             仅重启 FastAPI (5001)
  $0 5000             仅重启 Flask (5000)
  $0 -c               重启所有服务并清理旧日志备份
  $0 -f 5001          快速重启 FastAPI

EOF
}

# 解析参数
CLEAN_LOGS=false
FAST_MODE=false
TARGET_PORTS="5001 5000"

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -c|--clean-logs)
            CLEAN_LOGS=true
            shift
            ;;
        -f|--fast)
            FAST_MODE=true
            shift
            ;;
        5001|5000)
            TARGET_PORTS="$1"
            shift
            ;;
        *)
            echo -e "${RED}错误: 未知参数 '$1'${NC}"
            show_help
            exit 1
            ;;
    esac
done

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  GMS Auto Test 服务重启脚本${NC}"
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  目标端口: ${TARGET_PORTS}${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# 清理旧的日志备份
clean_old_logs() {
    echo -e "${YELLOW}清理旧日志备份（保留最近5个）...${NC}"
    for log in fastapi flask; do
        ls -t "${log}.log".* 2>/dev/null | tail -n +6 | xargs -r rm -f
    done
    echo -e "${GREEN}✓ 旧日志已清理${NC}"
}

# ==================== 1. 清理缓存 ====================
if [ "$FAST_MODE" = false ]; then
    echo -e "${YELLOW}[1/5] 清理 Python 缓存...${NC}"
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete 2>/dev/null || true
    echo -e "${GREEN}✓ Python 缓存已清理${NC}"
    echo ""
fi

# 清理旧日志备份
if [ "$CLEAN_LOGS" = true ]; then
    clean_old_logs
    echo ""
fi

# ==================== 2. 停止现有服务 ====================
echo -e "${YELLOW}[2/5] 停止现有服务...${NC}"

for port in $TARGET_PORTS; do
    if lsof -i :"$port" >/dev/null 2>&1; then
        echo -e "  停止 ${port} 端口服务..."
        fuser -k "$port/tcp" 2>/dev/null || true
        sleep 1

        # 确保进程已清理
        if lsof -i :"$port" >/dev/null 2>&1; then
            pids=$(lsof -t -i :"$port" 2>/dev/null || true)
            if [ -n "$pids" ]; then
                kill -9 $pids 2>/dev/null || true
                sleep 1
            fi
        fi

        echo -e "${GREEN}  ✓ ${port} 端口已停止${NC}"
    else
        echo -e "${BLUE}  ℹ ${port} 端口未运行${NC}"
    fi
done
echo ""

# ==================== 3. 清理日志文件 ====================
echo -e "${YELLOW}[3/5] 清理日志文件...${NC}"
for port in 5001 5000; do
    if echo "$TARGET_PORTS" | grep -q "$port"; then
        log_file=$([ "$port" = "5001" ] && echo "fastapi.log" || echo "flask.log")
        [ -f "$log_file" ] && mv "$log_file" "${log_file}.backup.$(date +%Y%m%d_%H%M%S)" 2>/dev/null || true
    fi
done
echo -e "${GREEN}✓ 日志已清理${NC}"
echo ""

# ==================== 4. 添加路由规则 ====================
echo -e "${YELLOW}[4/5] 添加路由规则...${NC}"
if sudo ip route add 10.10.10.0/24 via 172.16.14.1 2>/dev/null; then
    echo -e "${GREEN}  ✓ 路由规则添加成功: 10.10.10.0/24 via 172.16.14.1${NC}"
else
    if sudo ip route show | grep -q "10.10.10.0/24 via 172.16.14.1"; then
        echo -e "${BLUE}  ℹ 路由规则已存在: 10.10.10.0/24 via 172.16.14.1${NC}"
    else
        echo -e "${YELLOW}  ⚠ 路由规则添加失败，请检查权限${NC}"
    fi
fi
echo ""

# ==================== 5. 启动服务 ====================
echo -e "${YELLOW}[5/5] 启动服务...${NC}"

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

# 健康检查函数
health_check() {
    local port=$1
    local path=$2
    local max_attempts=5
    local attempt=1

    while [ $attempt -le $max_attempts ]; do
        if curl -s -f "http://localhost:${port}${path}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
        ((attempt++))
    done
    return 1
}

# 启动 FastAPI (5001)
if echo "$TARGET_PORTS" | grep -q "5001"; then
    echo -e "  启动 FastAPI (5001)..."
    nohup python3 -m uvicorn app_fastapi_full:app \
        --host 0.0.0.0 --port 5001 \
        --log-level info --access-log \
        >> fastapi.log 2>&1 &
    sleep 2

    if wait_for_port 5001; then
        if health_check 5001 "/api/system/health"; then
            echo -e "${GREEN}  ✓ FastAPI 启动成功（健康检查通过）${NC}"
        else
            echo -e "${YELLOW}  ⚠ FastAPI 启动成功但健康检查失败，请检查日志${NC}"
        fi
    else
        echo -e "${RED}  ✗ FastAPI 启动失败：tail -f fastapi.log${NC}"
        exit 1
    fi
fi

# 启动 Flask (5000)
if echo "$TARGET_PORTS" | grep -q "5000"; then
    echo -e "  启动 Flask (5000)..."
    nohup python3 app.py >> flask.log 2>&1 &
    sleep 2

    if wait_for_port 5000; then
        if health_check 5000 "/"; then
            echo -e "${GREEN}  ✓ Flask 启动成功（健康检查通过）${NC}"
        else
            echo -e "${YELLOW}  ⚠ Flask 启动成功但健康检查失败，请检查日志${NC}"
        fi
    else
        echo -e "${RED}  ✗ Flask 启动失败：tail -f flask.log${NC}"
        exit 1
    fi
fi
echo ""

# ==================== 完成 ====================
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}  ✓ 所有服务启动成功！${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "服务地址："
if echo "$TARGET_PORTS" | grep -q "5001"; then
    echo -e "  FastAPI: ${GREEN}http://localhost:5001${NC}"
fi
if echo "$TARGET_PORTS" | grep -q "5000"; then
    echo -e "  Flask:  ${GREEN}http://localhost:5000${NC}"
fi
echo ""
echo -e "常用命令："
echo -e "  查看日志: ${BLUE}tail -f fastapi.log${NC} ${BLUE}tail -f flask.log${NC}"
echo -e "  停止服务: ${BLUE}fuser -k 5001/tcp${NC} ${BLUE}fuser -k 5000/tcp${NC}"
echo -e "  查看端口: ${BLUE}lsof -i :5001${NC} ${BLUE}lsof -i :5000${NC}"
echo ""
