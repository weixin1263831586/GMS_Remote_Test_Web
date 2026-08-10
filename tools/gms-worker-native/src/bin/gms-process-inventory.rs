use std::io::{self, Read};

use gms_worker_native::process_inventory::{scan, ScanPayload};
use serde::Deserialize;
use serde_json::{json, Value};

const SCHEMA_VERSION: u64 = 1;

#[derive(Deserialize)]
struct Request {
    #[serde(default)]
    schema_version: u64,
    #[serde(default)]
    action: String,
    #[serde(default)]
    payload: Value,
}

fn execute() -> Result<Value, String> {
    let mut input = String::new();
    io::stdin()
        .read_to_string(&mut input)
        .map_err(|error| error.to_string())?;
    let request: Request = serde_json::from_str(&input).map_err(|error| error.to_string())?;
    if request.schema_version != SCHEMA_VERSION {
        return Err("unsupported process inventory contract version".to_string());
    }
    if request.action != "scan" {
        return Err(format!(
            "unsupported process inventory action: {}",
            request.action
        ));
    }
    let payload: ScanPayload =
        serde_json::from_value(request.payload).map_err(|error| error.to_string())?;
    let processes = scan(payload)?;
    Ok(json!({
        "schema_version": SCHEMA_VERSION,
        "success": true,
        "data": {"processes": processes},
    }))
}

fn main() {
    let response = execute().unwrap_or_else(|message| {
        json!({
            "schema_version": SCHEMA_VERSION,
            "success": false,
            "error": {
                "code": "PROCESS_INVENTORY_SCAN_FAILED",
                "message": message,
                "retryable": true,
                "details": {},
            },
        })
    });
    println!(
        "{}",
        serde_json::to_string(&response).unwrap_or_else(|_| "{}".to_string())
    );
}
