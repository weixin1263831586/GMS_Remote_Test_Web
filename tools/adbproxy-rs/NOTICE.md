# adbproxy-rs source and build artifact

- Upstream: <https://github.com/Ken-u/adbproxy-rs>
- GMS patched version: `0.4.5`
- Source commit: `f2beb4ff1bece8ab8f5d63c04dbfd6bf90aae8ee`
- Downstream patches: device policy is default-deny, and source connections
  require an explicit target-peer IP allowlist.
- Source archive: `adbproxy-rs-v0.4.5-source.tar.gz`
- Source SHA256: `f2d39f873636da56bec8039fe97530eeda24b459191ca97c87bd23839b344dea`
- License declared by upstream `Cargo.toml`: MIT

Run `scripts/build_adbproxy_rs.sh` on the Controller to compile the pinned
source into a static Linux x86_64 installation package under `dist/`. Worker
installation uses that package and does not access GitHub or require Rust.
