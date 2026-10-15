# Finite debug grants. Approval stays in the human browser session.
gms-rt-debug-session-request() {
    [ "$#" -ge 4 ] || return "$GMS_RT_EXIT_USAGE"
    local worker="$1" device="$2" package="$3" path="$4" minutes=60 operations='install,launch,stop,tap,swipe,text,keyevent'
    shift 4
    while [ "$#" -gt 0 ]; do
        [ "$#" -ge 2 ] || return "$GMS_RT_EXIT_USAGE"
        case "$1" in
            --minutes) minutes="$2" ;;
            --operations) operations="$2" ;;
            *) return "$GMS_RT_EXIT_USAGE" ;;
        esac
        shift 2
    done
    [[ "$minutes" =~ ^[0-9]+$ ]] && [ -f "$path" ] || return "$GMS_RT_EXIT_USAGE"
    path=$(realpath -- "$path") || return "$GMS_RT_EXIT_USAGE"
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
