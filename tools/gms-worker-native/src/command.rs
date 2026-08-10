use std::collections::BTreeMap;
use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::{Duration, Instant};

use wait_timeout::ChildExt;

use crate::contract::NativeError;

#[derive(Debug)]
pub struct CommandOutput {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
    pub error_code: Option<&'static str>,
}

const MAX_CAPTURE_BYTES: usize = 2 * 1024 * 1024;

struct CapturedOutput {
    bytes: Vec<u8>,
    truncated: bool,
}

fn read_limited(mut pipe: impl Read) -> std::io::Result<CapturedOutput> {
    let mut bytes = Vec::new();
    let mut truncated = false;
    let mut buffer = [0_u8; 8192];
    loop {
        let read = pipe.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        let remaining = MAX_CAPTURE_BYTES.saturating_sub(bytes.len());
        let retained = remaining.min(read);
        bytes.extend_from_slice(&buffer[..retained]);
        truncated |= retained < read;
    }
    Ok(CapturedOutput { bytes, truncated })
}

struct PipeReader {
    receiver: Receiver<std::io::Result<CapturedOutput>>,
    handle: thread::JoinHandle<()>,
}

impl PipeReader {
    fn spawn(pipe: impl Read + Send + 'static) -> Self {
        let (sender, receiver) = mpsc::sync_channel(1);
        let handle = thread::spawn(move || {
            let _ = sender.send(read_limited(pipe));
        });
        Self { receiver, handle }
    }

    fn receive(
        &self,
        deadline: Instant,
        program: &str,
        stream: &str,
    ) -> Result<CapturedOutput, NativeError> {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match self.receiver.recv_timeout(remaining) {
            Ok(Ok(output)) => Ok(output),
            Ok(Err(error)) => Err(NativeError::operation(format!(
                "failed to read {program} {stream}: {error}"
            ))),
            Err(RecvTimeoutError::Timeout) => Err(NativeError::timeout(format!(
                "{program} {stream} remained open after the command timeout"
            ))),
            Err(RecvTimeoutError::Disconnected) => Err(NativeError::operation(format!(
                "{program} {stream} reader stopped unexpectedly"
            ))),
        }
    }

    fn finish(self) {
        let _ = self.handle.join();
    }
}

fn terminate(child: &mut std::process::Child, process_group: i32) {
    #[cfg(unix)]
    unsafe {
        // The child was placed in a dedicated process group before spawn.
        libc::kill(-process_group, libc::SIGKILL);
    }
    let _ = child.kill();
    let _ = child.wait();
}

pub fn run(
    program: &str,
    args: &[String],
    timeout: Duration,
    environment: &BTreeMap<String, String>,
) -> Result<CommandOutput, NativeError> {
    let deadline = Instant::now() + timeout;
    let mut command = Command::new(program);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (name, value) in environment {
        command.env(name, value);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;

        command.process_group(0);
    }
    let mut child = command
        .spawn()
        .map_err(|error| NativeError::operation(format!("failed to start {program}: {error}")))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| NativeError::operation(format!("failed to capture {program} stdout")))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| NativeError::operation(format!("failed to capture {program} stderr")))?;
    let stdout_reader = PipeReader::spawn(stdout);
    let stderr_reader = PipeReader::spawn(stderr);
    let process_group = child.id() as i32;
    let remaining = deadline.saturating_duration_since(Instant::now());
    let status = match child.wait_timeout(remaining) {
        Err(error) => {
            terminate(&mut child, process_group);
            stdout_reader.finish();
            stderr_reader.finish();
            return Err(NativeError::operation(format!(
                "failed to wait for {program}: {error}"
            )));
        }
        Ok(Some(status)) => status,
        Ok(None) => {
            terminate(&mut child, process_group);
            stdout_reader.finish();
            stderr_reader.finish();
            return Err(NativeError::timeout(format!(
                "{program} timed out after {} seconds",
                timeout.as_secs()
            )));
        }
    };
    let stdout = match stdout_reader.receive(deadline, program, "stdout") {
        Ok(output) => output,
        Err(error) => {
            terminate(&mut child, process_group);
            stdout_reader.finish();
            stderr_reader.finish();
            return Err(error);
        }
    };
    let stderr = match stderr_reader.receive(deadline, program, "stderr") {
        Ok(output) => output,
        Err(error) => {
            terminate(&mut child, process_group);
            stdout_reader.finish();
            stderr_reader.finish();
            return Err(error);
        }
    };
    stdout_reader.finish();
    stderr_reader.finish();
    if stdout.truncated || stderr.truncated {
        return Err(NativeError::new(
            "NATIVE_COMMAND_OUTPUT_LIMIT",
            format!("{program} output exceeded the 2 MiB capture limit"),
            false,
            "Reduce command output and retry",
            serde_json::json!({
                "stdout_truncated": stdout.truncated,
                "stderr_truncated": stderr.truncated,
                "limit_bytes": MAX_CAPTURE_BYTES,
            }),
        ));
    }
    Ok(CommandOutput {
        code: status.code().unwrap_or(1),
        stdout: String::from_utf8_lossy(&stdout.bytes).into_owned(),
        stderr: String::from_utf8_lossy(&stderr.bytes).into_owned(),
        error_code: None,
    })
}

#[cfg(test)]
mod tests {
    use super::{run, MAX_CAPTURE_BYTES};
    use std::collections::BTreeMap;
    use std::time::{Duration, Instant};

    #[test]
    fn drains_output_while_the_child_is_running() {
        let output = run(
            "/bin/sh",
            &["-c".to_string(), "head -c 262144 /dev/zero".to_string()],
            Duration::from_secs(2),
            &BTreeMap::new(),
        )
        .unwrap();
        assert_eq!(output.stdout.len(), 262144);
    }

    #[test]
    fn rejects_output_larger_than_the_capture_limit() {
        let error = run(
            "/bin/sh",
            &[
                "-c".to_string(),
                format!("head -c {} /dev/zero", MAX_CAPTURE_BYTES + 1),
            ],
            Duration::from_secs(2),
            &BTreeMap::new(),
        )
        .unwrap_err();
        assert_eq!(error.code, "NATIVE_COMMAND_OUTPUT_LIMIT");
    }

    #[test]
    fn kills_the_process_group_after_timeout() {
        let started = Instant::now();
        let error = run(
            "/bin/sh",
            &["-c".to_string(), "sleep 5 & wait".to_string()],
            Duration::from_millis(50),
            &BTreeMap::new(),
        )
        .unwrap_err();
        assert_eq!(error.code, "USBIP_COMMAND_TIMEOUT");
        assert!(started.elapsed() < Duration::from_secs(2));
    }

    #[test]
    fn timeout_also_applies_to_inherited_output_pipes() {
        let started = Instant::now();
        let error = run(
            "/bin/sh",
            &["-c".to_string(), "sleep 5 &".to_string()],
            Duration::from_millis(50),
            &BTreeMap::new(),
        )
        .unwrap_err();
        assert_eq!(error.code, "USBIP_COMMAND_TIMEOUT");
        assert!(started.elapsed() < Duration::from_secs(2));
    }
}
