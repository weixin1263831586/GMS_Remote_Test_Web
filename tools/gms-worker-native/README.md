# GMS Worker native tools

This Cargo package contains two narrow, replaceable Worker executors:

- `gms-process-inventory`: read-only `/proc` Tradefed discovery.
- `gms-usbip-control`: USB/IP attach/detach data-plane execution using the
  root-owned `gms-worker-usbip` helper.

The Python Worker continues to own command acknowledgement, assignment
persistence, resource claims, authorization, operation locking, and generation
fencing. Both binaries receive one JSON request on standard input and emit one
JSON response on standard output. Diagnostics must use standard error.
Both executors are required at runtime; the Worker has no Python process-scan
or USB/IP data-plane fallback.

Build and test:

```bash
(cd tools/gms-worker-native && cargo test --locked)
scripts/build_gms_worker_native.sh
```

The build script writes binaries and `SHA256SUMS` beneath
`tools/gms-worker-native/dist/<architecture>/`. The Worker installer verifies
the checksums before installing matching artifacts. Deployment never compiles
Rust source on a Worker: release artifacts must be built ahead of time with the
toolchain pinned in `rust-toolchain.toml`.
