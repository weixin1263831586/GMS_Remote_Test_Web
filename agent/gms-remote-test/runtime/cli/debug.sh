# Finite debug grants. Approval stays in the human browser session.
_gms_debug_usage() {
    error "$1" >&2
    return "$GMS_RT_EXIT_USAGE"
}

gms-rt-debug-session-request() {
    [ "$#" -ge 4 ] || { _gms_debug_usage 'Usage: gms-rt-debug-session-request <worker_id> <device> <package> <local.apk|--installed> [--minutes N] [--operations OPS]'; return $?; }
    local worker="$1" device="$2" package="$3" path="$4" minutes=60 operations='install,launch,stop,tap,swipe,text,keyevent'
    local installed=0 operation
    if [ "$path" = '--installed' ]; then
        installed=1
        operations='launch,stop,tap,swipe,text,keyevent'
    fi
    shift 4
    while [ "$#" -gt 0 ]; do
        [ "$#" -ge 2 ] || { _gms_debug_usage "Missing value for $1"; return $?; }
        case "$1" in
            --minutes) minutes="$2" ;;
            --operations) operations="$2" ;;
            *) _gms_debug_usage "Unknown debug-session option: $1"; return $? ;;
        esac
        shift 2
    done
    if ! [[ "$minutes" =~ ^[0-9]{1,3}$ ]] || [ "$((10#$minutes))" -lt 15 ] || [ "$((10#$minutes))" -gt 120 ]; then
        _gms_debug_usage '--minutes must be an integer from 15 to 120'; return $?
    fi
    minutes=$((10#$minutes))
    local selected=()
    IFS=',' read -r -a selected <<< "$operations"
    if [ -z "$operations" ] || [[ "$operations" == *, ]]; then
        _gms_debug_usage '--operations requires a nonempty comma-separated list'; return $?
    fi
    for operation in "${selected[@]}"; do
        case "$operation" in
            install)
                if [ "$installed" -eq 1 ]; then
                    _gms_debug_usage '--installed cannot grant install; provide a local APK for installation'; return $?
                fi ;;
            launch|stop|tap|swipe|text|keyevent|database_read) ;;
            *) _gms_debug_usage "Unsupported debug operation: $operation"; return $? ;;
        esac
    done
    if [ "$installed" -eq 1 ]; then
        api_call '/devices/debug-sessions' POST '' \
            --form-string "worker_id=$worker" --form-string "device=$device" --form-string "package=$package" \
            --form-string "duration_minutes=$minutes" --form-string "operations=$operations" --form-string 'installed=true'
        return $?
    fi
    if [ ! -f "$path" ] || [ ! -r "$path" ]; then
        _gms_debug_usage 'apk_file must be a readable APK on the Agent host, not an Android device path. For a preinstalled app use --installed (MCP: installed=true) without apk_file.'; return $?
    fi
    path=$(realpath -- "$path") || { _gms_debug_usage 'Cannot resolve local APK path'; return $?; }
    path=${path//\\/\\\\}
    path=${path//\"/\\\"}
    api_call '/devices/debug-sessions' POST '' \
        --form-string "worker_id=$worker" --form-string "device=$device" --form-string "package=$package" \
        --form-string "duration_minutes=$minutes" --form-string "operations=$operations" \
        --form "apk_file=@\"$path\";type=application/vnd.android.package-archive"
}

gms-rt-debug-session-status() {
    [ "$#" -eq 1 ] && [[ "$1" =~ ^[a-f0-9]{32}$ ]] || return "$GMS_RT_EXIT_USAGE"
    api_call "/devices/debug-sessions/$1" GET
}

gms-rt-debug-session-revoke() {
    [ "$#" -eq 1 ] && [[ "$1" =~ ^[a-f0-9]{32}$ ]] || return "$GMS_RT_EXIT_USAGE"
    api_call "/devices/debug-sessions/$1/revoke" POST '{}'
}

gms-rt-debug-action() {
    [ "$#" -ge 2 ] && [[ "$1" =~ ^[a-f0-9]{32}$ ]] || return "$GMS_RT_EXIT_USAGE"
    local session="$1" action="$2" data field numeric
    shift 2
    case "$action" in launch|stop|tap|swipe|text|keyevent) ;; *) return "$GMS_RT_EXIT_USAGE" ;; esac
    data=$(jq -cn --arg action "$action" '{action:$action}') || return "$GMS_RT_EXIT_USAGE"
    while [ "$#" -gt 0 ]; do
        [ "$#" -ge 2 ] || return "$GMS_RT_EXIT_USAGE"
        field=${1#--}
        field=${field//-/_}
        numeric=false
        case "$1" in
            --x|--y|--end-x|--end-y|--duration-ms)
                [[ "$2" =~ ^[0-9]+$ ]] || return "$GMS_RT_EXIT_USAGE"
                numeric=true ;;
            --activity|--text|--key) ;;
            *) return "$GMS_RT_EXIT_USAGE" ;;
        esac
        data=$(jq -c --arg field "$field" --arg value "$2" --argjson numeric "$numeric" \
            '. + {($field): ($value | if $numeric then tonumber else . end)}' <<< "$data") || return "$GMS_RT_EXIT_USAGE"
        shift 2
    done
    api_call "/devices/debug-sessions/$session/actions" POST "$data"
}
