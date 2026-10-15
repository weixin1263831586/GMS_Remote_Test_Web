# Typed APK deployment and read-only database diagnosis.
gms-rt-apk-deploy() {
    [ "$#" -ge 3 ] || {
        error 'Usage: gms-rt-apk-deploy <worker_id> <device> <apk_file> [--no-replace] [--debug-session ID]'
        return "$GMS_RT_EXIT_USAGE"
    }
    local worker="$1" device="$2" path="$3" replace=true session=''
    shift 3
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --no-replace) replace=false; shift ;;
            --debug-session)
                [ "$#" -ge 2 ] && [[ "$2" =~ ^[a-f0-9]{32}$ ]] || return "$GMS_RT_EXIT_USAGE"
                session="$2"; shift 2 ;;
            *) return "$GMS_RT_EXIT_USAGE" ;;
        esac
    done
    [ -f "$path" ] || { error 'APK file not found'; return "$GMS_RT_EXIT_USAGE"; }
    path=$(realpath -- "$path") || return "$GMS_RT_EXIT_USAGE"
    path=${path//\\/\\\\}
    path=${path//\"/\\\"}
    api_call '/devices/deployments' POST '' \
        --form-string "worker_id=$worker" --form-string "device=$device" \
        --form-string "replace_existing=$replace" \
        --form-string "debug_session_id=$session" \
        --form "apk_file=@\"$path\";type=application/vnd.android.package-archive"
}

gms-rt-apk-deployment-status() {
    [ "$#" -eq 1 ] && [[ "$1" =~ ^[a-f0-9]{32}$ ]] || return "$GMS_RT_EXIT_USAGE"
    api_call "/devices/deployments/$1" GET
}

gms-rt-devices-db-query() {
    [ "$#" -ge 4 ] || return "$GMS_RT_EXIT_USAGE"
    local worker="$1" device="$2" package="$3" database="$4"
    shift 4
    local table='' columns='' limit=100 offset=0 session=''
    if [ "$#" -gt 0 ] && [[ "$1" != --* ]]; then table="$1"; shift; fi
    while [ "$#" -gt 0 ]; do
        [ "$#" -ge 2 ] || return "$GMS_RT_EXIT_USAGE"
        case "$1" in
            --debug-session) session="$2" ;;
            --columns) columns="$2" ;;
            --limit) limit="$2" ;;
            --offset) offset="$2" ;;
            *) return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift 2
    done
    [[ "$session" =~ ^[a-f0-9]{32}$ ]] || return "$GMS_RT_EXIT_USAGE"
    [[ "$limit" =~ ^[0-9]+$ ]] && [[ "$offset" =~ ^[0-9]+$ ]] || return "$GMS_RT_EXIT_USAGE"
    local data
    data=$(jq -cn --arg worker "$worker" --arg device "$device" --arg package "$package" \
        --arg session "$session" --arg database "$database" --arg table "$table" --arg columns "$columns" \
        --argjson limit "$limit" --argjson offset "$offset" \
        '{debug_session_id:$session,worker_id:$worker,device:$device,package:$package,database:$database,table:$table,
          columns:($columns|split(",")|map(select(length>0))),limit:$limit,offset:$offset}') || return "$GMS_RT_EXIT_USAGE"
    api_call '/devices/db/query' POST "$data"
}
