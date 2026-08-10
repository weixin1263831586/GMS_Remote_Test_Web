use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

pub const SCHEMA_VERSION: u64 = 1;

#[derive(Debug, Deserialize)]
pub struct Request {
    #[serde(default)]
    pub schema_version: u64,
    #[serde(default)]
    pub transport: String,
    #[serde(default)]
    pub action: String,
    #[serde(default)]
    pub payload: Value,
}

#[derive(Debug)]
pub struct NativeError {
    pub code: &'static str,
    pub message: String,
    pub retryable: bool,
    pub remediation: String,
    pub details: Value,
}

impl NativeError {
    pub fn new(
        code: &'static str,
        message: impl Into<String>,
        retryable: bool,
        remediation: impl Into<String>,
        details: Value,
    ) -> Self {
        Self {
            code,
            message: message.into(),
            retryable,
            remediation: remediation.into(),
            details,
        }
    }

    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new(
            "TRANSPORT_EXECUTOR_INVALID_REQUEST",
            message,
            false,
            "",
            json!({}),
        )
    }

    pub fn operation(message: impl Into<String>) -> Self {
        Self::new("USBIP_OPERATION_FAILED", message, false, "", json!({}))
    }

    pub fn timeout(message: impl Into<String>) -> Self {
        Self::new(
            "USBIP_COMMAND_TIMEOUT",
            message,
            true,
            "Check usbipd, ADB, and the Worker helper service",
            json!({}),
        )
    }
}

impl std::fmt::Display for NativeError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for NativeError {}

#[derive(Serialize)]
pub struct ErrorBody<'a> {
    pub code: &'a str,
    pub message: &'a str,
    pub retryable: bool,
    pub remediation: &'a str,
    pub details: &'a Value,
}

pub fn error_response(error: &NativeError) -> Value {
    json!({
        "schema_version": SCHEMA_VERSION,
        "success": false,
        "error": ErrorBody {
            code: error.code,
            message: &error.message,
            retryable: error.retryable,
            remediation: &error.remediation,
            details: &error.details,
        },
    })
}
