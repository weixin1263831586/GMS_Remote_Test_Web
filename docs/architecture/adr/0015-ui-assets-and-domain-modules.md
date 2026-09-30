# ADR 0015: Allowlisted UI assets and domain modules

Status: Accepted

## Decision

Redmine UI resources use `/redmine-agent/assets/{asset_name}`. An explicit
filename-to-media-type allowlist serves only the required JavaScript and CSS;
HTML, Python sources, maps, directory paths and unlisted files return 404.
Resources retain `no-store` and `nosniff` headers and the existing application
authentication middleware. Asset routes are excluded from OpenAPI.

The HTML declares classic scripts in dependency order. Shared helpers load
first, domain implementations follow, and `page.js` performs initialization
last. Statistics, issue presentation, knowledge, settings, single-issue
diagnosis and live timelines have separate ownership. Each script stays within
the default 50 KB frontend budget. Splitting does not add server writes during
page initialization or change the ModalManager visibility boundary.

Previously published resource URLs remain allowlisted compatibility entries.
Legacy `page.js` serves the split page implementation as a bundle, excluding
the helper scripts that old HTML already loads separately. Adding an internal
asset changes the allowlist and HTML rather than adding a business API route.

The Agent CLI loads fixed `runtime/cli/*.sh` command domains from its resolved
package directory. Caller input never selects module paths. Installed command
links and contract tooling derive their inventory from modules explicitly
sourced by the entrypoint; older monolithic packages remain supported. The
package lifecycle ships the modules and regenerates the plugin from canonical
sources as one version.

Module boundaries follow complete responsibilities, rather than one endpoint
family per file. Authentication includes enrollment/approval; cluster commands
include device ownership and transport; platform support includes configuration,
users, connectivity and self-check; dispatch includes catalog and help. Small
adjacent sections share their owning module. The UI likewise keeps shared
presentation helpers together, and smoke-test files group related scenarios.

Browser smoke cases live under `tests/runtime_ui/`, grouped by UI domain with
a shared harness. The former module keeps compatibility imports; CI collects
the new directory so moving cases cannot silently drop coverage.
Test groups use responsibility names, not numbered size-based chunks: device
inventory and actions stay together, terminal and serial consoles share a
group, and agent access has one owner. Reports and artifact analysis share
their upload/task lifecycle group. Tests are not split solely to reach a line
target, and small regressions extend their existing owning test modules.

## Verification

Allowlist/path rejection and legacy URL tests, frontend syntax and size gates,
browser smoke collection and repeated navigation, CLI contract and installer
tests, source/generated package parity, and deliberate contract regeneration.
