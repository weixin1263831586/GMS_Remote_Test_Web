use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;
use std::thread;
use std::time::Duration;

use regex::Regex;
use serde::Deserialize;
use serde_json::{json, Map, Value};

use crate::command::{run, CommandOutput};
use crate::contract::NativeError;

static SOURCE_HOST: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9._:-]{1,255}$").unwrap());
static BUSID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9._-]{1,64}$").unwrap());
static PORT_HEADER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\s*Port\s+(\d+):").unwrap());

#[derive(Debug, Deserialize)]
pub struct UsbipPayload {
    #[serde(default)]
    pub source_host: String,
    #[serde(default)]
    pub busids: Vec<String>,
    #[serde(default)]
    pub adb_server_socket: String,
    #[serde(default)]
    pub generation: u64,
}

pub struct OperationResult {
    pub data: Value,
    pub transport_state: &'static str,
    pub protocol_state: &'static str,
    pub readiness: &'static str,
    pub generation: u64,
}

#[derive(Debug)]
struct StepFailure {
    code: String,
    exit_code: i32,
    message: String,
}

impl StepFailure {
    fn from_output(output: &CommandOutput, fallback: &str) -> Self {
        Self {
            code: output
                .error_code
                .unwrap_or("USBIP_HELPER_EXITED")
                .to_string(),
            exit_code: output.code,
            message: output_error(output, fallback),
        }
    }

    fn as_json(&self) -> Value {
        json!({
            "code": self.code,
            "exit_code": self.exit_code,
            "message": self.message,
        })
    }
}

fn failure_map(errors: &BTreeMap<String, StepFailure>) -> Value {
    Value::Object(
        errors
            .iter()
            .map(|(key, error)| (key.clone(), error.as_json()))
            .collect(),
    )
}

fn port_query_error(output: &CommandOutput, context: &str) -> NativeError {
    NativeError::new(
        "USBIP_PORT_QUERY_FAILED",
        output_error(output, "usbip port failed"),
        true,
        "Verify the Worker USB/IP helper, kernel vhci driver, and sudo policy",
        json!({
            "context": context,
            "exit_code": output.code,
            "helper_error_code": output.error_code,
        }),
    )
}

fn validate(payload: &UsbipPayload) -> Result<Vec<String>, NativeError> {
    if !SOURCE_HOST.is_match(&payload.source_host) {
        return Err(NativeError::invalid("invalid USB/IP source host"));
    }
    let mut selected = Vec::new();
    for raw in &payload.busids {
        let value = raw.trim();
        if !BUSID.is_match(value) {
            return Err(NativeError::invalid(format!(
                "invalid USB/IP busid: {value}"
            )));
        }
        selected.push(value.to_string());
    }
    if selected.is_empty() {
        return Err(NativeError::invalid(
            "at least one USB/IP busid is required",
        ));
    }
    Ok(selected)
}

fn output_error(output: &CommandOutput, fallback: &str) -> String {
    let value = if output.stderr.trim().is_empty() {
        output.stdout.trim()
    } else {
        output.stderr.trim()
    };
    if value.is_empty() {
        fallback.to_string()
    } else {
        value.to_string()
    }
}

fn busy_retry_delay() -> Duration {
    let milliseconds = std::env::var("GMS_USBIP_BUSY_RETRY_DELAY_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(2000)
        .min(10_000);
    Duration::from_millis(milliseconds)
}

fn helper(action: &str, args: &[String]) -> CommandOutput {
    let path = std::env::var("GMS_WORKER_USBIP_HELPER")
        .unwrap_or_else(|_| "/usr/local/libexec/gms-worker-usbip".to_string());
    let mut command_args = vec!["-n".to_string(), path, action.to_string()];
    command_args.extend_from_slice(args);
    run(
        "sudo",
        &command_args,
        Duration::from_secs(10),
        &BTreeMap::new(),
    )
    .unwrap_or_else(|error| CommandOutput {
        code: if error.code == "USBIP_COMMAND_TIMEOUT" {
            124
        } else {
            125
        },
        stdout: String::new(),
        stderr: error.message,
        error_code: Some(error.code),
    })
}

pub fn port_blocks(output: &str) -> Vec<(String, String)> {
    let mut blocks = Vec::new();
    let mut port = String::new();
    let mut lines = Vec::new();
    for raw in output.lines().chain(std::iter::once("Port 999999:")) {
        if let Some(captures) = PORT_HEADER.captures(raw) {
            if !port.is_empty() {
                blocks.push((port.clone(), lines.join("\n")));
            }
            port = captures[1].to_string();
            lines = vec![raw.to_string()];
        } else if !port.is_empty() {
            lines.push(raw.to_string());
        }
    }
    blocks
}

fn url_host_and_path(raw: &str) -> Option<(String, String)> {
    let value = raw
        .trim_end_matches(|character: char| [',', ';', ')'].contains(&character))
        .strip_prefix("usbip://")?;
    let (authority, path) = value.split_once('/')?;
    let host = if let Some(bracketed) = authority.strip_prefix('[') {
        bracketed.split_once(']')?.0.to_string()
    } else {
        authority
            .rsplit_once(':')
            .map(|(host, _port)| host)
            .unwrap_or(authority)
            .to_string()
    };
    Some((host, path.trim_matches('/').to_string()))
}

pub fn port_matches(block: &str, source_host: &str, busid: &str) -> bool {
    block.split_whitespace().any(|token| {
        url_host_and_path(token).is_some_and(|(host, path)| host == source_host && path == busid)
    })
}

fn command_output(
    program: &str,
    args: &[&str],
    timeout: Duration,
    adb_server_socket: Option<&str>,
) -> Result<CommandOutput, NativeError> {
    let environment = adb_server_socket
        .filter(|value| !value.is_empty())
        .map(|value| BTreeMap::from([("ADB_SERVER_SOCKET".to_string(), value.to_string())]))
        .unwrap_or_default();
    run(
        program,
        &args.iter().map(|item| item.to_string()).collect::<Vec<_>>(),
        timeout,
        &environment,
    )
}

fn probe_details(serial: &str, adb_server_socket: Option<&str>) -> Map<String, Value> {
    let Ok(output) = command_output(
        "adb",
        &[
            "-s",
            serial,
            "shell",
            "echo __MODEL__; getprop ro.product.model; echo __ANDROID__; getprop ro.build.version.release; echo __BATTERY__; dumpsys battery | grep '^  level:' | head -n 1; echo __SOC__; getprop ro.soc.model",
        ],
        Duration::from_secs(3),
        adb_server_socket,
    ) else {
        return Map::new();
    };
    let markers = BTreeMap::from([
        ("__MODEL__", "model"),
        ("__ANDROID__", "android_version"),
        ("__BATTERY__", "battery_level"),
        ("__SOC__", "soc_model"),
    ]);
    let mut details = Map::new();
    let mut current = "";
    for raw in output.stdout.lines() {
        let value = raw.trim();
        if let Some(marker) = markers.get(value) {
            current = marker;
            continue;
        }
        if current.is_empty() || value.is_empty() || details.contains_key(current) {
            continue;
        }
        let value = if current == "battery_level" {
            value
                .split_once(':')
                .map(|(_, level)| level.trim())
                .unwrap_or(value)
        } else {
            value
        };
        details.insert(current.to_string(), json!(value));
    }
    details
}

fn probe_devices(include_details: bool, adb_server_socket: Option<&str>) -> Vec<Value> {
    let mut devices = Vec::new();
    let output = command_output(
        "adb",
        &["devices", "-l"],
        Duration::from_secs(10),
        adb_server_socket,
    );
    if let Ok(output) = output {
        for line in output.stdout.lines().skip(1) {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() < 2 || !["device", "offline", "unauthorized"].contains(&parts[1]) {
                continue;
            }
            let serial = parts[0];
            if serial.starts_with("localhost:") {
                continue;
            }
            let mut properties = Map::new();
            for item in parts.iter().skip(2) {
                if let Some((key, value)) = item.split_once(':') {
                    properties.insert(key.to_string(), json!(value));
                }
            }
            if include_details && parts[1] == "device" {
                properties.extend(probe_details(serial, adb_server_socket));
            }
            devices.push(json!({
                "serial": serial,
                "transport": "local_usb",
                "state": if parts[1] == "device" { "available" } else { parts[1] },
                "properties": properties,
            }));
        }
    }
    let known: BTreeSet<String> = devices
        .iter()
        .filter_map(|item| {
            item.get("serial")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    if let Ok(output) = command_output("fastboot", &["devices"], Duration::from_secs(10), None) {
        for line in output.stdout.lines() {
            let Some(serial) = line.split_whitespace().next() else {
                continue;
            };
            if !serial.is_empty() && !known.contains(serial) {
                devices.push(json!({
                    "serial": serial,
                    "transport": "local_usb",
                    "state": "fastboot",
                    "properties": {},
                }));
            }
        }
    }
    devices
}

fn device_serials(devices: &[Value]) -> BTreeSet<String> {
    devices
        .iter()
        .filter_map(|item| {
            item.get("serial")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect()
}

fn probe_until_settled() -> Vec<Value> {
    for _ in 0..6 {
        let devices = probe_devices(true, None);
        if !devices.iter().any(|item| {
            matches!(
                item.get("state").and_then(Value::as_str),
                Some("offline" | "unauthorized")
            )
        }) {
            return devices;
        }
        thread::sleep(Duration::from_secs(1));
    }
    probe_devices(true, None)
}

fn attach(payload: &UsbipPayload, selected: &[String]) -> Result<OperationResult, NativeError> {
    let adb_socket =
        (!payload.adb_server_socket.is_empty()).then_some(payload.adb_server_socket.as_str());
    let before = device_serials(&probe_devices(false, adb_socket));
    let mut attached = Vec::new();
    let mut already_attached = Vec::new();
    let mut newly_attached = Vec::new();
    let mut errors: BTreeMap<String, StepFailure> = BTreeMap::new();
    let current_ports = helper("port", &[]);
    if current_ports.code != 0 {
        return Err(port_query_error(&current_ports, "attach"));
    }
    let blocks = port_blocks(&current_ports.stdout);
    for busid in selected {
        if blocks
            .iter()
            .any(|(_, block)| port_matches(block, &payload.source_host, busid))
        {
            attached.push(busid.clone());
            already_attached.push(busid.clone());
            continue;
        }
        for attempt in 0..3 {
            let output = helper("attach", &[payload.source_host.clone(), busid.clone()]);
            if output.code == 0 {
                attached.push(busid.clone());
                newly_attached.push(busid.clone());
                break;
            }
            let error = StepFailure::from_output(&output, "USB/IP attach failed");
            let lower = error.message.to_ascii_lowercase();
            errors.insert(busid.clone(), error);
            if !lower.contains("busy") || !lower.contains("exported") || attempt == 2 {
                break;
            }
            thread::sleep(busy_retry_delay());
        }
        if attached.contains(busid) {
            errors.remove(busid);
        } else {
            break;
        }
    }
    if !errors.is_empty() && !attached.is_empty() {
        let ports = helper("port", &[]);
        let mut rollback_errors: BTreeMap<String, StepFailure> = BTreeMap::new();
        let mut rolled_back = BTreeSet::new();
        if ports.code == 0 {
            for (port, block) in port_blocks(&ports.stdout) {
                let matching: Vec<&String> = newly_attached
                    .iter()
                    .filter(|busid| port_matches(&block, &payload.source_host, busid))
                    .collect();
                if matching.is_empty() {
                    continue;
                }
                let detached = helper("detach", std::slice::from_ref(&port));
                if detached.code != 0 {
                    rollback_errors.insert(
                        port.clone(),
                        StepFailure::from_output(&detached, &format!("port {port} detach failed")),
                    );
                } else {
                    rolled_back.extend(matching.into_iter().cloned());
                }
            }
            let missing: Vec<&String> = newly_attached
                .iter()
                .filter(|busid| !rolled_back.contains(*busid))
                .collect();
            if !missing.is_empty() {
                rollback_errors.insert(
                    "unresolved_busids".to_string(),
                    StepFailure {
                        code: "USBIP_ROLLBACK_PORT_NOT_FOUND".to_string(),
                        exit_code: 1,
                        message: format!(
                            "未找到待回滚端口: {}",
                            missing.into_iter().cloned().collect::<Vec<_>>().join(", ")
                        ),
                    },
                );
            }
        } else {
            rollback_errors.insert(
                "port_query".to_string(),
                StepFailure::from_output(&ports, "usbip port failed during rollback"),
            );
        }
        let mut details = errors
            .iter()
            .map(|(busid, error)| format!("{busid}: {}", error.message))
            .collect::<Vec<_>>()
            .join("; ");
        if !rollback_errors.is_empty() {
            details.push_str("; 回滚未完成: ");
            details.push_str(
                &rollback_errors
                    .values()
                    .map(|error| error.message.clone())
                    .collect::<Vec<_>>()
                    .join("; "),
            );
        }
        let rollback_failed = !rollback_errors.is_empty();
        return Err(NativeError::new(
            if rollback_failed {
                "USBIP_ROLLBACK_FAILED"
            } else {
                "USBIP_ATTACH_PARTIAL"
            },
            if rollback_failed {
                format!("USB/IP接入未全部成功且回滚未完成: {details}")
            } else {
                format!("USB/IP接入未全部成功，本次新增接入已回滚: {details}")
            },
            !rollback_failed,
            if rollback_failed {
                "Inspect `usbip port` on this Worker and detach leaked ports before retrying"
            } else {
                "Retry the complete USB/IP selection"
            },
            json!({
                "attach_errors": failure_map(&errors),
                "rollback_errors": failure_map(&rollback_errors),
                "newly_attached_busids": newly_attached,
            }),
        ));
    }
    if attached.is_empty() {
        let details = errors
            .iter()
            .map(|(busid, error)| format!("{busid}: {}", error.message))
            .collect::<Vec<_>>()
            .join("; ");
        if errors.values().any(|error| {
            let lower = error.message.to_ascii_lowercase();
            lower.contains("busy") && lower.contains("exported")
        }) {
            return Err(NativeError::new(
                "USBIP_EXPORT_BUSY",
                format!(
                    "USB设备仍被其他Worker或残留USB/IP会话占用；请先在原接入主机断开后重试。{details}"
                ),
                true,
                "Detach the export from its previous Worker or clear the stale USB/IP session",
                json!({"attach_errors": failure_map(&errors)}),
            ));
        }
        let timed_out = errors
            .values()
            .any(|error| error.code == "USBIP_COMMAND_TIMEOUT");
        return Err(NativeError::new(
            if timed_out {
                "USBIP_ATTACH_TIMEOUT"
            } else {
                "USBIP_ATTACH_FAILED"
            },
            if details.is_empty() {
                "USB/IP attach failed".to_string()
            } else {
                details
            },
            true,
            "Check source usbipd reachability, export state, and the Worker helper",
            json!({"attach_errors": failure_map(&errors)}),
        ));
    }
    if already_attached.len() == selected.len() && newly_attached.is_empty() {
        return Ok(OperationResult {
            data: json!({
                "attached_busids": attached,
                "already_attached_busids": already_attached,
                "errors": {},
                "devices": probe_devices(true, adb_socket),
                "new_devices": [],
                "enumeration_pending": false,
            }),
            transport_state: "attached",
            protocol_state: "adb",
            readiness: "test_ready",
            generation: payload.generation,
        });
    }
    let mut devices = Vec::new();
    let mut new_serials = BTreeSet::new();
    for _ in 0..15 {
        thread::sleep(Duration::from_secs(1));
        devices = probe_devices(true, adb_socket);
        new_serials = device_serials(&devices)
            .difference(&before)
            .cloned()
            .collect();
        if !new_serials.is_empty() {
            break;
        }
    }
    let ready = !new_serials.is_empty();
    Ok(OperationResult {
        data: json!({
            "attached_busids": attached,
            "already_attached_busids": already_attached,
            "errors": {},
            "devices": devices,
            "new_devices": new_serials,
            "enumeration_pending": !ready,
        }),
        transport_state: "attached",
        protocol_state: if ready { "adb" } else { "enumerating" },
        readiness: if ready {
            "test_ready"
        } else {
            "transport_ready"
        },
        generation: payload.generation,
    })
}

fn detach(payload: &UsbipPayload, selected: &[String]) -> Result<OperationResult, NativeError> {
    let ports = helper("port", &[]);
    if ports.code != 0 {
        return Err(port_query_error(&ports, "detach"));
    }
    let matched: Vec<String> = port_blocks(&ports.stdout)
        .into_iter()
        .filter_map(|(port, block)| {
            selected
                .iter()
                .any(|busid| port_matches(&block, &payload.source_host, busid))
                .then_some(port)
        })
        .collect();
    if matched.is_empty() {
        return Ok(OperationResult {
            data: json!({
                "detached_ports": [],
                "already_detached": true,
                "devices": probe_devices(true, None),
            }),
            transport_state: "disconnected",
            protocol_state: "unknown",
            readiness: "not_ready",
            generation: payload.generation,
        });
    }
    let mut detached = Vec::new();
    let mut detach_errors = BTreeMap::new();
    for port in &matched {
        let output = helper("detach", std::slice::from_ref(port));
        if output.code == 0 {
            detached.push(port.clone());
        } else {
            detach_errors.insert(
                port.clone(),
                StepFailure::from_output(&output, &format!("port {port} detach failed")),
            );
        }
    }
    if detached.len() != matched.len() {
        return Err(NativeError::new(
            "USBIP_DETACH_PARTIAL",
            "部分USB/IP端口断开失败",
            true,
            "Inspect `usbip port` and retry detach for the remaining ports",
            json!({
                "detached_ports": detached,
                "detach_errors": failure_map(&detach_errors),
            }),
        ));
    }
    Ok(OperationResult {
        data: json!({
            "detached_ports": detached,
            "devices": probe_until_settled(),
        }),
        transport_state: "disconnected",
        protocol_state: "unknown",
        readiness: "not_ready",
        generation: payload.generation,
    })
}

pub fn execute(action: &str, payload: UsbipPayload) -> Result<OperationResult, NativeError> {
    let selected = validate(&payload)?;
    match action {
        "attach" => attach(&payload, &selected),
        "detach" => detach(&payload, &selected),
        _ => Err(NativeError::invalid(format!(
            "unsupported USB/IP action: {action}"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::{port_blocks, port_matches, validate, UsbipPayload};

    #[test]
    fn matches_exact_source_and_busid() {
        let output = "Port 00: <Port in Use>\n       usbip://192.0.2.10:3240/1-10\n";
        let blocks = port_blocks(output);
        assert_eq!(blocks.len(), 1);
        assert!(port_matches(&blocks[0].1, "192.0.2.10", "1-10"));
        assert!(!port_matches(&blocks[0].1, "192.0.2.10", "1-1"));
        assert!(!port_matches(&blocks[0].1, "192.0.2.11", "1-10"));
    }

    #[test]
    fn rejects_shell_metacharacters() {
        let payload = UsbipPayload {
            source_host: "192.0.2.10;touch".to_string(),
            busids: vec!["1-2".to_string()],
            adb_server_socket: String::new(),
            generation: 0,
        };
        assert!(validate(&payload).is_err());
    }
}
