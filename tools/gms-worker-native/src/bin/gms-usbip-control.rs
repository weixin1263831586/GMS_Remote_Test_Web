use std::io::{self, Read};

use gms_worker_native::contract::{error_response, NativeError, Request, SCHEMA_VERSION};
use gms_worker_native::usbip::{execute as execute_usbip, UsbipPayload};
use serde_json::{json, Value};

fn execute() -> Result<Value, NativeError> {
    let mut input = String::new();
    io::stdin()
        .read_to_string(&mut input)
        .map_err(|error| NativeError::invalid(error.to_string()))?;
    let request: Request =
        serde_json::from_str(&input).map_err(|error| NativeError::invalid(error.to_string()))?;
    if request.schema_version != SCHEMA_VERSION {
        return Err(NativeError::invalid(
            "unsupported transport contract version",
        ));
    }
    if request.transport != "usbip" {
        return Err(NativeError::invalid(format!(
            "unsupported transport: {}",
            request.transport
        )));
    }
    let payload: UsbipPayload = serde_json::from_value(request.payload)
        .map_err(|error| NativeError::invalid(error.to_string()))?;
    let result = execute_usbip(&request.action, payload)?;
    Ok(json!({
        "schema_version": SCHEMA_VERSION,
        "success": true,
        "transport_state": result.transport_state,
        "protocol_state": result.protocol_state,
        "readiness": result.readiness,
        "generation": result.generation,
        "data": result.data,
    }))
}

fn main() {
    let response = match execute() {
        Ok(response) => response,
        Err(error) => error_response(&error),
    };
    println!(
        "{}",
        serde_json::to_string(&response).unwrap_or_else(|_| "{}".to_string())
    );
}
