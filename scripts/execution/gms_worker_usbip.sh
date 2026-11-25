#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
value1="${2:-}"
value2="${3:-}"

case "${action}" in
    attach)
        [[ "${value1}" =~ ^[A-Za-z0-9._:-]{1,255}$ ]] || exit 2
        [[ "${value2}" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || exit 2
        /sbin/modprobe vhci_hcd
        exec /usr/bin/usbip attach -r "${value1}" -b "${value2}"
        ;;
    detach)
        [[ "${value1}" =~ ^[0-9]{1,6}$ ]] || exit 2
        exec /usr/bin/usbip detach -p "${value1}"
        ;;
    port)
        exec /usr/bin/usbip port
        ;;
    *)
        echo "usage: gms-worker-usbip attach HOST BUSID | detach PORT | port" >&2
        exit 2
        ;;
esac
