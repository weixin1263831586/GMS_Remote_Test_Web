#!/bin/bash
set -euo pipefail

DEVICE=""
SYSTEM_IMG=""
VENDOR_IMG=""

# 解析命令行参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --system)
            shift
            SYSTEM_IMG="$1"
            ;;
        --vendor)
            shift
            VENDOR_IMG="$1"
            ;;
        *)
            if [[ -z "$DEVICE" ]]; then
                DEVICE="$1"
            else
                echo "未知参数: $1" >&2
                exit 1
            fi
            ;;
    esac
    shift
done

if [[ -z "$DEVICE" ]] || [[ -z "$SYSTEM_IMG" ]]; then
    echo "Usage: $0 <device> --system <system.img> [--vendor <vendor_boot.img>]" >&2
    exit 1
fi

if [[ ! -f "$SYSTEM_IMG" ]]; then
    echo "❌ System 镜像不存在: $SYSTEM_IMG" >&2
    exit 1
fi

echo "🔄 重启设备 $DEVICE 进入 bootloader..."
adb -s "$DEVICE" reboot bootloader
sleep 5

echo "🔓 尝试解锁 vboot（如果已解锁会跳过）..."
if fastboot -s "$DEVICE" oem at-unlock-vboot 2>/dev/null; then
    echo "✅ vboot 解锁成功，等待设备重启..."
    sleep 8  # 增加等待时间，让设备完全重启
else
    echo "⚠️ vboot 解锁失败或已解锁，继续执行"
    sleep 2
fi

# 确保设备在fastboot模式
echo "📱 确保设备在fastboot模式..."
if ! fastboot -s "$DEVICE" devices | grep -q "$DEVICE"; then
    echo "⚠️ 设备未在fastboot模式，尝试重启..."
    adb -s "$DEVICE" reboot bootloader
    sleep 5
fi

echo "🗑️ 删除 product 分区（如果存在）..."
for partition in product product_a product_b; do
    if fastboot -s "$DEVICE" delete-logical-partition "$partition" 2>/dev/null; then
        echo "✅ 已删除分区: $partition"
    else
        echo "⚠️ 分区 $partition 不存在或删除失败，跳过"
    fi
done

echo "💾 烧写 system 镜像..."
fastboot -s "$DEVICE" flash system "$SYSTEM_IMG"

fastboot -s "$DEVICE" flash misc /home/hcq/GMS-Suite/misc.img

if [[ -n "$VENDOR_IMG" ]]; then
    if [[ -f "$VENDOR_IMG" ]]; then
        echo "💾 烧写 vendor_boot 镜像..."
        fastboot -s "$DEVICE" flash vendor_boot "$VENDOR_IMG"
    else
        echo "⚠️ Vendor boot 镜像不存在，跳过: $VENDOR_IMG"
    fi
fi

echo "🔄 重启设备..."
fastboot -s "$DEVICE" reboot

echo "✅ GSI 烧写完成!"