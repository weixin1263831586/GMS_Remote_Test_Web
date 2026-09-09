# Code Search plugin

This plugin is self-contained: it bundles the codesearch client
(`scripts/codesearch.py`) and exposes it as two stdio MCP tools, `search`
and `projects` (injected as `mcp__codesearch__search` /
`mcp__codesearch__projects`).

Use it instead of local grep when exploring large repos (AOSP, kernels):
the remote index resolves definitions, references, and paths in seconds.

Layout:

```text
codesearch/
├── kk.plugin.json
├── config/config.json      # service URL, token, default projects
└── scripts/
    ├── codesearch.py       # bundled search client
    └── mcp_server.py       # stdio MCP adapter
```

Install it by copying this directory to:

```text
~/.kkagent/plugins/codesearch/
```

Then restart kkagent or run `/plugins reload`.

## Search tool (0.8.1)

| Parameter | Meaning |
|---|---|
| `keywords` | One token or comma-separated tokens; wrap an exact multi-word phrase in double quotes, e.g. `"bind to service"` |
| `search_field` | `smart` (default; picks def/symbol/path/full automatically), `def`, `symbol`, `path`, `full` |
| `path` | Path scope AND-matched against file paths, e.g. `frameworks/base`, `drivers/android`. Works with every field; use it to narrow a previous hit to a subsystem |
| `project` | Comma-separated project names (defaults from `config.json`) |
| `type` | File type filter: c, cxx, java, kotlin, python, sh, golang, rust |
| `limit` | Max files returned (default 15) |

Changes in 0.8.1 (over 0.8.0):

- Backend switched to the new OpenGrok 1.14.17 instance
  `https://codeindex.rock-chips.com/source` (same `/api/v1` REST contract);
  bundled token updated and `default_projects` changed to `Android17`.
  The old `http://10.10.10.203:8080/source` instance is decommissioned and
  remains only as the documented env-var override example. No client-side
  search behavior or output format change.

Changes in 0.8.0 (over 0.7.0):
- Plan aggregation now consumes requests in completion order (`as_completed`): a single slow backend request no longer delays aggregation of the ones already finished.
- `analyze_query` is cached (`lru_cache`), removing repeated regex parsing in per-file scoring/filtering loops on wide searches.
- MCP adapter runs the bundled client in-process per tool call (no interpreter startup per call); falls back to the isolated subprocess automatically if in-process loading or execution fails.
- `plain_search` now reports raw counts like the def/symbol path; removed a dead validation branch; dropped an unused aggregation lock.

Changes in 0.7.0 (over 0.6.0):

- early-stop actually fires now: plan requests are re-ordered so primaries
  run before secondaries (previously everything landed in one parallel batch
  and the definition check never skipped anything); simple tokens demote
  symbol/full to secondary, so `getService`-style lookups skip them once a
  definition is found (~1s wall, was ~3s reported / ~1.5s wall)
- `time_ms` now reports the plan's wall clock instead of the sum of
  per-request server times (parallel requests were double-counted)

Changes in 0.6.0 (over 0.5.0):

- user `limit` is pushed down to the OpenGrok backend (`maxresults` scales
  with it, plus `maxHitsPerFile=20`), cutting broad-term smart searches to
  sub-second wall time
- path-like queries (e.g. `services/am/ActivityManagerService.java`) are no
  longer misparsed as FQN class+member; they resolve to the target file in
  ~0.3 s with correct ranking
- quoted phrases now produce strictly phrase-matched results in every mode
  (previously some modes degraded to token-OR)
- invalid `type`/`search_field` values fail loudly with the supported list
  instead of returning empty results

Output notes:

- `result_count` counts deduplicated files; the raw match-line total across
  index queries is printed as a comment when it differs.
- Within a file, definition-like and query-matching lines are shown first,
  capped at 8 lines per file.

Performance: smart queries fan out to parallel index requests (early-stopped
once a definition is found), so typical lookups finish in 1–3 s.

The bundled `config/config.json` carries the defaults (`base_url`, `token`,
`default_projects`, `default_limit`). They can be overridden through
environment variables without touching the plugin:

```bash
export RK_CODESEARCH_URL=http://codesearch.example.com/source
export RK_CODESEARCH_TOKEN=replace-me
```

The plugin manifest is `kk.plugin.json` with an inline `mcpServers`
declaration. Python 3 must be available as `python3` on `PATH`; Windows users
whose installation only exposes `python.exe` should set the manifest command
to `python` in their installed copy.
