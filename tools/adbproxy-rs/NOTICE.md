# adbproxy-rs source and build artifact

- Upstream: <https://github.com/Ken-u/adbproxy-rs>
- GMS patched version: `0.4.5`
- Source commit: `f2beb4ff1bece8ab8f5d63c04dbfd6bf90aae8ee`
- Downstream patches: device policy is default-deny, source connections
  require an explicit target-peer IP allowlist, and the shared hub retains
  port 5037 while its local ADB side server starts. Linux peer UID lookup also
  supports half-closed Tradefed connections in `FIN_WAIT1`/`FIN_WAIT2` and is
  performed immediately after `accept()` to avoid losing short-lived clients
  before their `inet_diag` owner lookup. Multipart `NLM_F_DUMP` responses are
  read through `NLMSG_DONE`, so high connection counts cannot hide the matching
  client beyond the first netlink receive buffer. A `--single-user` mode uses
  the classic in-process aggregator on Linux and keeps a managed hub alive
  across `host:kill`; GMS Remote Test uses this mode because each worker and
  its CTS/GTS/VTS/STS processes run under one OS account. The single-user hub
  reserves TCP/5037 before starting its side ADB server or polling remote
  backends, preventing concurrent inventory probes from auto-starting the
  stock ADB server and stealing the hub port during startup.
- Source archive: `adbproxy-rs-v0.4.5-source.tar.gz`
- Source SHA256: `347a1885fcd36cc721287d1f124370dacef8e2e1e2649d4f6c73516a87bf4d06`
- License declared by upstream `Cargo.toml`: MIT

Run `scripts/build_adbproxy_rs.sh` on the Controller to compile the pinned
source into a static Linux x86_64 installation package under `dist/`. Worker
installation uses that package and does not access GitHub or require Rust.
