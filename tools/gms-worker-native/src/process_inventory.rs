use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::UNIX_EPOCH;

use chrono::{DateTime, SecondsFormat, Utc};
use regex::Regex;
use serde::{Deserialize, Serialize};

static TRADEFED_LAUNCHER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:cts|gts|vts|sts)-tradefed$").unwrap());
static RUNTIME_INFO: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?P<path>/[^\s:]+/tf_runtime_info)(?::|$)").unwrap());
static RUNTIME_ACTIVITY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?P<path>/[^\s:]+/(?:tf_runtime_info|tf_test_module_results))(?::|$)").unwrap()
});
static TEST_ID: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"/test_(?P<id>[0-9a-f-]{20,})/").unwrap());
static DEVICE_SERIAL: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$").unwrap());
static ACTIVE_CONSOLE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\bCompatibilityConsole\s+run\b").unwrap());

#[derive(Debug, Deserialize)]
pub struct ScanPayload {
    #[serde(default)]
    pub managed_pids: Vec<i32>,
    #[serde(default = "default_proc_root")]
    pub proc_root: String,
    #[serde(default)]
    pub now: Option<f64>,
    #[serde(default = "default_stall_seconds")]
    pub stall_seconds: u64,
}

fn default_proc_root() -> String {
    "/proc".to_string()
}

fn default_stall_seconds() -> u64 {
    3600
}

#[derive(Debug, Clone)]
struct Process {
    proc_dir: PathBuf,
    pid: i32,
    ppid: i32,
    argv: Vec<String>,
    comm: String,
    cpu_ticks: u64,
    start_ticks: u64,
}

#[derive(Debug, Serialize)]
pub struct ProcessRecord {
    worker_job_id: String,
    job_id: String,
    attempt_id: String,
    status: String,
    pid: i32,
    devices: Vec<String>,
    source: String,
    suite_type: String,
    command: String,
    started_at: String,
    elapsed_seconds: u64,
    cpu_percent: f64,
    rss_mb: f64,
    process_count: usize,
    log_path: String,
    last_output_age_seconds: Option<u64>,
    warning: String,
}

fn parse_stat(raw: &str) -> Result<(i32, u64, u64), String> {
    let close = raw
        .rfind(')')
        .ok_or_else(|| "process stat has no closing parenthesis".to_string())?;
    let fields: Vec<&str> = raw[close + 1..].split_whitespace().collect();
    if fields.len() <= 19 {
        return Err("process stat is truncated".to_string());
    }
    let parse = |index: usize| {
        fields[index]
            .parse::<u64>()
            .map_err(|error| format!("invalid process stat field: {error}"))
    };
    Ok((
        fields[1]
            .parse::<i32>()
            .map_err(|error| format!("invalid process parent pid: {error}"))?,
        parse(11)? + parse(12)?,
        parse(19)?,
    ))
}

fn read_process_minimal(path: &Path, pid: i32) -> Result<Process, String> {
    let raw_stat = fs::read_to_string(path.join("stat")).map_err(|error| error.to_string())?;
    let (ppid, cpu_ticks, start_ticks) = parse_stat(&raw_stat)?;
    let raw_argv = fs::read(path.join("cmdline")).map_err(|error| error.to_string())?;
    let argv: Vec<String> = raw_argv
        .split(|byte| *byte == 0)
        .filter(|item| !item.is_empty())
        .map(|item| String::from_utf8_lossy(item).into_owned())
        .collect();
    if argv.is_empty() {
        return Err("empty process argv".to_string());
    }
    let comm = fs::read_to_string(path.join("comm"))
        .map_err(|error| error.to_string())?
        .trim()
        .to_string();
    Ok(Process {
        proc_dir: path.to_path_buf(),
        pid,
        ppid,
        argv,
        comm,
        cpu_ticks,
        start_ticks,
    })
}

fn read_rss_kb(process: &Process) -> u64 {
    fs::read_to_string(process.proc_dir.join("status"))
        .ok()
        .and_then(|status| {
            status.lines().find_map(|line| {
                line.strip_prefix("VmRSS:")?
                    .split_whitespace()
                    .next()?
                    .parse::<u64>()
                    .ok()
            })
        })
        .unwrap_or(0)
}

fn basename(value: &str) -> &str {
    value.rsplit('/').next().unwrap_or(value)
}

fn is_tradefed(argv: &[String], comm: &str) -> bool {
    if TRADEFED_LAUNCHER.is_match(&comm.to_ascii_lowercase()) {
        return true;
    }
    if argv
        .iter()
        .any(|item| TRADEFED_LAUNCHER.is_match(&basename(item).to_ascii_lowercase()))
    {
        return true;
    }
    let joined = argv.join(" ").to_ascii_lowercase();
    joined.contains("tradefed.jar") || joined.contains("compatibilityconsole")
}

fn looks_like_serial(value: &str) -> bool {
    let value = value.trim();
    if value.is_empty() || value.starts_with('-') {
        return false;
    }
    let lower = value.to_ascii_lowercase();
    if [".jar", ".js", ".json", ".py", ".sh", ".txt"]
        .iter()
        .any(|suffix| lower.ends_with(suffix))
    {
        return false;
    }
    DEVICE_SERIAL.is_match(value)
}

fn extract_devices(argv: &[String]) -> BTreeSet<String> {
    let mut devices = BTreeSet::new();
    for pair in argv.windows(2) {
        if ["-s", "--serial", "--device-serial"].contains(&pair[0].as_str())
            && looks_like_serial(&pair[1])
        {
            devices.insert(pair[1].clone());
        }
    }
    let joined = argv.join(" ");
    for captures in RUNTIME_INFO.captures_iter(&joined) {
        let Some(path) = captures.name("path") else {
            continue;
        };
        let Ok(contents) = fs::read_to_string(path.as_str()) else {
            continue;
        };
        let Ok(payload) = serde_json::from_str::<serde_json::Value>(&contents) else {
            continue;
        };
        let Some(invocations) = payload.get("invocations").and_then(|item| item.as_array()) else {
            continue;
        };
        for invocation in invocations {
            let Some(ids) = invocation.get("deviceIds").and_then(|item| item.as_array()) else {
                continue;
            };
            for id in ids.iter().filter_map(|item| item.as_str()) {
                if looks_like_serial(id) {
                    devices.insert(id.to_string());
                }
            }
        }
    }
    devices
}

fn suite_type(argv: &[String]) -> String {
    let joined = argv.join(" ").to_ascii_lowercase();
    for name in ["cts", "gts", "vts", "sts"] {
        if joined.contains(&format!("{name}-tradefed"))
            || joined.contains(&format!("android-{name}"))
        {
            return name.to_ascii_uppercase();
        }
    }
    "XTS".to_string()
}

fn find_log_path(argv: &[String], cwd: &Path) -> Option<PathBuf> {
    for item in argv.iter().filter(|item| item.ends_with(".log")) {
        let candidate = PathBuf::from(item);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    let joined = argv.join(" ");
    let test_id = TEST_ID
        .captures(&joined)
        .and_then(|captures| captures.name("id"))
        .map(|value| value.as_str())?;
    if cwd.file_name().and_then(|value| value.to_str()) != Some("tools") {
        return None;
    }
    let logs = cwd.parent()?.join("logs");
    for first in fs::read_dir(logs).ok()?.flatten() {
        for second in fs::read_dir(first.path()).ok()?.flatten() {
            let candidate = second
                .path()
                .join(format!("TradefedTest_test_{test_id}"))
                .join("xts_tf_output.log");
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    None
}

fn has_ancestor(processes: &HashMap<i32, Process>, mut pid: i32, ancestors: &HashSet<i32>) -> bool {
    let mut seen = HashSet::new();
    while pid > 1 && seen.insert(pid) {
        if ancestors.contains(&pid) {
            return true;
        }
        pid = processes.get(&pid).map(|item| item.ppid).unwrap_or(0);
    }
    false
}

fn adb_descendant_argv(
    processes: &HashMap<i32, Process>,
    group_pids: &HashSet<i32>,
) -> Vec<String> {
    let mut argv = Vec::new();
    for (pid, process) in processes {
        if group_pids.contains(pid) || !process.comm.starts_with("adb") {
            continue;
        }
        if has_ancestor(processes, *pid, group_pids) {
            argv.extend(process.argv.clone());
        }
    }
    argv
}

fn is_active_invocation(argv: &[String]) -> bool {
    let joined = argv.join(" ");
    RUNTIME_INFO.is_match(&joined) || ACTIVE_CONSOLE.is_match(&joined)
}

fn file_mtime(path: &Path) -> Option<f64> {
    fs::metadata(path)
        .ok()?
        .modified()
        .ok()?
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|duration| duration.as_secs_f64())
}

fn round2(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}

fn truncate_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

pub fn scan(payload: ScanPayload) -> Result<Vec<ProcessRecord>, String> {
    let proc_root = Path::new(&payload.proc_root);
    let uptime = fs::read_to_string(proc_root.join("uptime"))
        .ok()
        .and_then(|value| value.split_whitespace().next()?.parse::<f64>().ok())
        .unwrap_or(0.0);
    let clock_ticks = unsafe { libc::sysconf(libc::_SC_CLK_TCK) }.max(1) as f64;
    let now = if let Some(now) = payload.now {
        now
    } else {
        std::time::SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| error.to_string())?
            .as_secs_f64()
    };
    let mut processes = HashMap::new();
    let entries = fs::read_dir(proc_root).map_err(|error| error.to_string())?;
    for entry in entries.flatten() {
        let Some(name) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        let Ok(pid) = name.parse::<i32>() else {
            continue;
        };
        if let Ok(process) = read_process_minimal(&entry.path(), pid) {
            processes.insert(pid, process);
        }
    }

    let matched: HashSet<i32> = processes
        .iter()
        .filter_map(|(pid, process)| is_tradefed(&process.argv, &process.comm).then_some(*pid))
        .collect();
    let mut groups: BTreeMap<i32, Vec<i32>> = BTreeMap::new();
    for pid in &matched {
        let mut root = *pid;
        let mut parent = processes.get(&root).map(|item| item.ppid).unwrap_or(0);
        let mut seen = HashSet::new();
        while parent > 1 && seen.insert(parent) {
            if matched.contains(&parent) {
                root = parent;
            }
            parent = processes.get(&parent).map(|item| item.ppid).unwrap_or(0);
        }
        groups.entry(root).or_default().push(*pid);
    }

    let managed: HashSet<i32> = payload.managed_pids.into_iter().collect();
    let mut result = Vec::new();
    for (root_pid, member_pids) in groups {
        let members: Vec<&Process> = member_pids
            .iter()
            .filter_map(|pid| processes.get(pid))
            .collect();
        if members
            .iter()
            .any(|process| has_ancestor(&processes, process.pid, &managed))
        {
            continue;
        }
        let mut argv: Vec<String> = members
            .iter()
            .flat_map(|process| process.argv.clone())
            .collect();
        if !is_active_invocation(&argv) {
            let group_pids: HashSet<i32> = member_pids
                .iter()
                .copied()
                .chain(std::iter::once(root_pid))
                .collect();
            let adb_argv = adb_descendant_argv(&processes, &group_pids);
            if adb_argv.is_empty() {
                continue;
            }
            argv.extend(adb_argv);
        }
        let devices: Vec<String> = extract_devices(&argv).into_iter().collect();
        let start_ticks = members
            .iter()
            .map(|item| item.start_ticks)
            .min()
            .unwrap_or(0);
        let elapsed = if uptime > 0.0 {
            (uptime - start_ticks as f64 / clock_ticks).max(0.0)
        } else {
            0.0
        };
        let cpu_ticks: u64 = members.iter().map(|item| item.cpu_ticks).sum();
        let cpu_percent = if elapsed > 0.0 {
            100.0 * cpu_ticks as f64 / clock_ticks / elapsed
        } else {
            0.0
        };
        let Some(root) = processes.get(&root_pid) else {
            continue;
        };
        let root_cwd = fs::read_link(root.proc_dir.join("cwd")).unwrap_or_default();
        let mut log_path = find_log_path(&argv, &root_cwd);
        let mut log_age = log_path
            .as_ref()
            .and_then(|path| file_mtime(path))
            .map(|mtime| (now - mtime).max(0.0));
        if log_path.is_some() && log_age.is_none() {
            log_path = None;
        }
        let activity_mtimes: Vec<f64> = RUNTIME_ACTIVITY
            .captures_iter(&argv.join(" "))
            .filter_map(|captures| captures.name("path"))
            .filter_map(|path| file_mtime(Path::new(path.as_str())))
            .collect();
        if let Some(latest) = activity_mtimes.into_iter().reduce(f64::max) {
            let activity_age = (now - latest).max(0.0);
            log_age = Some(
                log_age
                    .map(|age| age.min(activity_age))
                    .unwrap_or(activity_age),
            );
        }
        let warning = if devices.is_empty() {
            "Tradefed is running but its device could not be identified".to_string()
        } else if log_age.is_some_and(|age| age >= payload.stall_seconds as f64) {
            format!(
                "Tradefed output has been inactive for {} seconds; the current module may be long-running or stalled",
                log_age.unwrap_or(0.0) as u64
            )
        } else {
            String::new()
        };
        let started_at = if elapsed > 0.0 {
            let timestamp = now - elapsed;
            let timestamp_micros = (timestamp * 1_000_000.0).round() as i64;
            DateTime::<Utc>::from_timestamp_micros(timestamp_micros)
                .map(|value| value.to_rfc3339_opts(SecondsFormat::AutoSi, false))
                .unwrap_or_default()
        } else {
            String::new()
        };
        result.push(ProcessRecord {
            worker_job_id: format!("external-{root_pid}-{start_ticks}"),
            job_id: String::new(),
            attempt_id: String::new(),
            status: "running".to_string(),
            pid: root_pid,
            devices,
            source: "external".to_string(),
            suite_type: suite_type(&argv),
            command: truncate_chars(&root.argv.join(" "), 1000),
            started_at,
            elapsed_seconds: elapsed as u64,
            cpu_percent: round2(cpu_percent),
            rss_mb: round2(
                members.iter().map(|item| read_rss_kb(item)).sum::<u64>() as f64 / 1024.0,
            ),
            process_count: members.len(),
            log_path: log_path
                .as_ref()
                .map(|path| path.to_string_lossy().into_owned())
                .unwrap_or_default(),
            last_output_age_seconds: log_age.map(|age| age as u64),
            warning,
        });
    }
    result.sort_by(|left, right| (&left.started_at, left.pid).cmp(&(&right.started_at, right.pid)));
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::{
        adb_descendant_argv, extract_devices, is_active_invocation, is_tradefed, looks_like_serial,
        Process,
    };
    use std::collections::{HashMap, HashSet};
    use std::path::PathBuf;

    #[test]
    fn rejects_script_paths_as_serials() {
        assert!(!looks_like_serial("agent.js"));
        assert!(looks_like_serial("192.0.2.10:5555"));
        let argv = vec![
            "frida".to_string(),
            "-s".to_string(),
            "agent.js".to_string(),
            "--serial".to_string(),
            "SERIAL".to_string(),
        ];
        assert_eq!(
            extract_devices(&argv).into_iter().collect::<Vec<_>>(),
            ["SERIAL"]
        );
    }

    #[test]
    fn recognizes_tradefed_launcher_and_jar() {
        assert!(is_tradefed(
            &["/suite/tools/cts-tradefed".to_string()],
            "bash"
        ));
        assert!(is_tradefed(
            &["java".to_string(), "tradefed.jar".to_string()],
            "java"
        ));
    }

    #[test]
    fn distinguishes_active_and_idle_tradefed_consoles() {
        assert!(!is_active_invocation(&[
            "java".to_string(),
            "com.android.compatibility.common.tradefed.command.CompatibilityConsole".to_string(),
        ]));
        assert!(is_active_invocation(&[
            "java".to_string(),
            "CompatibilityConsole".to_string(),
            "run".to_string(),
            "cts".to_string(),
        ]));
    }

    #[test]
    fn finds_adb_descendant_of_interactive_console() {
        let process = |pid, ppid, comm: &str, argv: &[&str]| Process {
            proc_dir: PathBuf::new(),
            pid,
            ppid,
            argv: argv.iter().map(|item| item.to_string()).collect(),
            comm: comm.to_string(),
            cpu_ticks: 0,
            start_ticks: 0,
        };
        let processes = HashMap::from([
            (100, process(100, 1, "vts-tradefed", &["./vts-tradefed"])),
            (101, process(101, 100, "java", &["java", "tradefed.jar"])),
            (
                102,
                process(
                    102,
                    101,
                    "adb",
                    &["adb", "-s", "INTERACTIVE-SERIAL", "shell"],
                ),
            ),
        ]);
        let argv = adb_descendant_argv(&processes, &HashSet::from([100, 101]));
        assert!(argv.iter().any(|item| item == "INTERACTIVE-SERIAL"));
        assert!(extract_devices(&argv).contains("INTERACTIVE-SERIAL"));
    }
}
