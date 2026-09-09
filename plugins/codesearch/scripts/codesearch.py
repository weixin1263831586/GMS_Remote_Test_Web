#!/usr/bin/env python3
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


import argparse
import functools
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


DEFAULT_BASE_URL = "https://codeindex.rock-chips.com/source"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config/config.json"
PATH_EXTENSIONS = (".java", ".kt", ".aidl", ".cpp", ".cc", ".c", ".h", ".hpp")
DEFINITION_KEYWORDS = ("class", "interface", "enum", "object", "struct", "typedef")
DEFAULT_LIMIT = 15
MAX_MATCHES_PER_FILE = 8
MAX_PARALLEL_REQUESTS = 8
MAX_FILES_PER_REQUEST = 200
# Backend caps hits per file during collection; keep headroom above the
# display cap so definition-line classification still sees the key lines.
MAX_HITS_PER_FILE = 20
MATCH_KIND_RANK = {
    "member-definition": 0,
    "class-definition": 0,
    "package": 1,
    "member-reference": 2,
    "class-reference": 2,
    "text": 3,
}
FILE_TYPE_MAP = {
    "c": "C",
    "cxx": "C++",
    "java": "Java",
    "kotlin": "Kotlin",
    "python": "Python",
    "sh": "Shell script",
    "golang": "Golang",
    "rust": "Rust",
}
KEYWORD_MODE_MAP = {
    "and": "AND",
    "or": "OR",
}


_CONFIG_CACHE = None


def load_config():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        _CONFIG_CACHE = _load_config()
    return _CONFIG_CACHE


def _load_config():
    data = {}
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    base_url = os.environ.get("RK_CODESEARCH_URL") or os.environ.get("OPENGROK_URL") or data.get("base_url") or DEFAULT_BASE_URL
    token = os.environ.get("RK_CODESEARCH_TOKEN") or os.environ.get("OPENGROK_TOKEN") or data.get("token", "")
    return {
        "base_url": base_url.rstrip("/"),
        "token": token.strip(),
        "default_projects": data.get("default_projects", []),
        "default_limit": parse_default_limit(data.get("default_limit", DEFAULT_LIMIT)),
    }


def parse_default_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return limit if limit > 0 else DEFAULT_LIMIT


def request(path, params=None, accept="application/json"):
    cfg = load_config()
    if not cfg["token"]:
        raise ValueError("missing code search token; set config/config.json or RK_CODESEARCH_TOKEN")

    url = f"{cfg['base_url']}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {cfg['token']}")
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "remote-run-plugin/rk_codesearch")
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def list_projects():
    projects = list_projects_data()
    for project in projects:
        print(project)


def list_projects_data():
    return json.loads(request("/api/v1/projects"))


def parse_projects(projects, default_projects):
    if projects:
        return [item.strip() for item in projects.split(",") if item.strip()]
    if isinstance(default_projects, str):
        return [default_projects.strip()] if default_projects.strip() else []
    if isinstance(default_projects, list):
        return [str(item).strip() for item in default_projects if str(item).strip()]
    return []


SEARCH_FIELDS = {"smart", "full", "def", "symbol", "path"}


def normalize_search_field(search_field):
    field = (search_field or "smart").strip().lower()
    field_map = {
        "smart": "smart",
        "auto": "smart",
        "full": "full",
        "path": "path",
        "def": "def",
        "defs": "def",
        "symbol": "symbol",
        "ref": "symbol",
        "refs": "symbol",
    }
    if field not in field_map:
        raise ValueError(f"unsupported search field: {search_field}")
    return field_map[field]


def build_search_params(query, search_field, projects, file_type, path_scope="", max_files=None):
    cfg = load_config()
    if not query:
        raise ValueError("search requires --query")
    field = normalize_search_field(search_field)
    if field == "smart":
        raise ValueError("smart search does not map to one request")

    path_terms = normalize_path_scope(path_scope)
    if field == "path":
        # OpenGrok path terms are AND-ed: merge scope terms into the query.
        params = {field: f"{query} {path_terms}".strip()}
    else:
        params = {field: query}
        if path_terms:
            # full/def/symbol combine with path= as a separate AND filter.
            params["path"] = path_terms

    selected_projects = parse_projects(projects, cfg.get("default_projects", []))

    if selected_projects:
        params["projects"] = selected_projects
    if file_type:
        params["type"] = normalize_file_type(file_type)
    # Cap what the backend collects/highlights per request: the raw match-line
    # total of broad terms dominates latency, and we only display `limit` files.
    params["maxresults"] = max_files or MAX_FILES_PER_REQUEST
    params["maxHitsPerFile"] = MAX_HITS_PER_FILE
    return params


def plan_fetch_cap(limit):
    """Files to fetch per index request inside multi-request plans: enough to
    keep dedup/ranking meaningful after aggregation, bounded by the hard cap."""
    return max(50, min(resolve_limit(limit) * 4, MAX_FILES_PER_REQUEST))


def plain_fetch_cap(limit):
    """Files to fetch for single-request fields."""
    return max(30, min(resolve_limit(limit) * 2, MAX_FILES_PER_REQUEST))


def parse_keywords(keywords):
    """Split keywords on commas; double-quoted phrases keep their spaces."""
    normalized = (keywords or "").strip()
    if not normalized:
        return []
    values = []
    for item in normalized.split(","):
        item = item.strip()
        if not item:
            continue
        phrase = re.fullmatch(r'"([^"]+)"', item)
        if phrase:
            values.append(phrase.group(1))
            continue
        if '"' in item:
            raise ValueError('double quotes must wrap the whole phrase, e.g. "bind to service"')
        if re.search(r"\s", item):
            raise ValueError(
                'multi-word phrases must be wrapped in double quotes, e.g. '
                '"bind to service"; separate keywords with commas'
            )
        values.append(item)
    return values


def is_phrase(item):
    return bool(re.search(r"\s", item))


def ensure_quoted(term):
    """Wrap in double quotes unless already quoted; used for phrase full-text."""
    stripped = (term or "").strip()
    if stripped.startswith('"') and stripped.endswith('"'):
        return stripped
    return f'"{stripped}"'


def normalize_keyword_mode(keyword_mode):
    mode = (keyword_mode or "and").strip().lower()
    if mode not in KEYWORD_MODE_MAP:
        allowed = ", ".join(KEYWORD_MODE_MAP.keys())
        raise ValueError(f"unsupported keyword mode: {keyword_mode}. supported values: {allowed}")
    return KEYWORD_MODE_MAP[mode]


def build_effective_query(keywords, keyword_mode):
    """Return (query, is_multi_keyword)."""
    keyword_items = parse_keywords(keywords)
    if not keyword_items:
        raise ValueError("search requires --keywords")

    def render(item):
        return f'"{item}"' if is_phrase(item) else item

    if len(keyword_items) == 1:
        return render(keyword_items[0]), False
    operator = normalize_keyword_mode(keyword_mode)
    return f" {operator} ".join(render(item) for item in keyword_items), True


def normalize_path_scope(path_scope):
    """Normalize a path scope into space-separated AND terms.

    'frameworks/base/' -> 'frameworks base'; surrounding quotes are stripped.
    """
    value = (path_scope or "").strip().strip('"').strip("'").replace("\\", "/")
    parts = [part for part in value.split("/") if part]
    return " ".join(parts)


def normalize_file_type(file_type):
    normalized = (file_type or "").strip().lower()
    if not normalized:
        return ""
    if normalized not in FILE_TYPE_MAP:
        allowed = ", ".join(FILE_TYPE_MAP.keys())
        raise ValueError(f"unsupported file type: {file_type}. supported values: {allowed}")
    return FILE_TYPE_MAP[normalized]


def looks_like_qualified_name(query):
    return bool(re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", query or ""))


def looks_like_path(query):
    return "/" in (query or "") or "\\" in (query or "")


@functools.lru_cache(maxsize=256)
def analyze_query(query):
    # Returns a shared read-only dict; callers must not mutate it.
    raw = (query or "").strip()
    slash_normalized = raw.replace("\\", "/")

    # Path-like queries (contain '/') are NOT FQNs: 'foo/bar/Baz.java' would
    # otherwise parse as class 'foo' + member 'bar' and fan out wild searches.
    if looks_like_path(raw):
        dot_parts = []
        package_parts = []
        segments = [part for part in slash_normalized.split("/") if part]
        # Anchor classification/ranking on the filename stem, e.g.
        # 'a/b/Baz.java' -> 'Baz', so the target file outranks incidental
        # path-token matches (Android.bp etc.).
        stem = segments[-1].rsplit(".", 1)[0] if segments else ""
        symbol_parts = [stem] if stem else []
    else:
        dot_parts = [part for part in raw.split(".") if part]

        package_parts = []
        symbol_parts = []
        switched = False
        for part in dot_parts:
            if not switched and re.fullmatch(r"[a-z_]\w*", part):
                package_parts.append(part)
                continue
            switched = True
            symbol_parts.append(part)

        if not symbol_parts and dot_parts:
            symbol_parts = [dot_parts[-1]]
            package_parts = dot_parts[:-1]

    class_name = symbol_parts[0] if symbol_parts else ""
    member_name = symbol_parts[1] if len(symbol_parts) > 1 else ""
    package_name = ".".join(package_parts)
    package_path = "/".join(package_parts)
    normalized_path = package_path
    if class_name:
        normalized_path = f"{package_path}/{class_name}" if package_path else class_name

    return {
        "raw": raw,
        "lower": raw.lower(),
        "path_normalized": slash_normalized,
        "package_parts": package_parts,
        "package_name": package_name,
        "package_path": package_path,
        "symbol_parts": symbol_parts,
        "class_name": class_name,
        "member_name": member_name,
        "last_token": re.split(r"[./\\]", raw)[-1] if raw else "",
        "looks_like_fqn": len(package_parts) > 0 and bool(class_name),
        "looks_like_member_query": bool(member_name),
        "looks_like_class_query": len(package_parts) > 0 and bool(class_name) and not member_name,
        "looks_like_simple_symbol": bool(re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?", raw)),
        "normalized_symbol_path": normalized_path,
    }


def build_smart_plan(query, file_type):
    info = analyze_query(query)
    plan = []
    seen = set()

    def add(field, term, weight, category, priority="primary"):
        key = (field, term)
        if not term or key in seen:
            return
        seen.add(key)
        plan.append({
            "field": field,
            "query": term,
            "weight": weight,
            "category": category,
            "priority": priority,
        })

    # Exact phrases (already quoted by build_effective_query): only the full
    # field understands them; def/symbol/path are single-term fields and a
    # double-wrapped quote would degrade the backend query to token OR.
    if is_phrase(query.strip().strip('"')):
        add("full", ensure_quoted(query), 70, "text")
        add("full", info["last_token"], 50, "text", priority="secondary")
        return plan

    # Path-like queries first: 'a/b/C.java' must not be treated as an FQN
    # (it would fan out to wild symbol/full searches on path segments).
    if looks_like_path(query):
        add("path", info["path_normalized"], 140, "path")
        add("full", f"\"{query}\"", 90, "text")
        add("full", query, 60, "text", priority="secondary")
        return plan

    if info["looks_like_member_query"]:
        # One path=class request covers every file extension (token match);
        # normalized_symbol_path is kept for FQN queries that pin the package.
        add("path", info["normalized_symbol_path"], 170, "path")
        add("path", info["class_name"], 165, "path")
        add("full", f"\"{query}\"", 160, "member-definition")
        add("def", info["class_name"], 150, "class-definition")
        add("symbol", info["member_name"], 130, "member-reference")
        add("full", f"\"{info['class_name']}.{info['member_name']}\"", 120, "member-reference")
        add("full", info["member_name"], 90, "member-reference", priority="secondary")
        return plan

    if info["looks_like_class_query"]:
        add("path", info["normalized_symbol_path"], 150, "path")
        if "." not in info["last_token"]:
            add("path", info["last_token"], 145, "path")
        add("def", info["class_name"], 130, "class-definition")
        add("symbol", info["class_name"], 110, "class-reference")
        add("full", f"\"{query}\"", 90, "class-reference")
        add("full", query, 60, "class-reference", priority="secondary")
        return plan

    add("def", query, 120, "definition")
    add("symbol", query, 100, "reference", priority="secondary")
    add("path", query, 80, "path")
    add("full", f"\"{query}\"", 70, "text", priority="secondary")
    add("full", query, 50, "text", priority="secondary")
    return plan


def score_result(path, matches, original_query, plan_item):
    info = analyze_query(original_query)
    score = plan_item["weight"]
    lower_path = path.lower()
    query_lower = info["lower"]
    last_token = info["last_token"].lower()
    normalized_path_query = info["normalized_symbol_path"].lower()
    simple_symbol = info["raw"].lower() if info["looks_like_simple_symbol"] else ""

    if last_token and re.search(rf"\b{re.escape(last_token)}\b", Path(path).name.lower()):
        score += 30
    if info["looks_like_fqn"] and normalized_path_query in lower_path:
        score += 80
    if info["looks_like_fqn"] and info["class_name"] and path_matches_class_file(path, info["class_name"]):
        score += 120
    if looks_like_path(original_query) and original_query.replace("\\", "/").lower() in lower_path:
        score += 80
    if info["class_name"] and re.search(rf"\b{re.escape(info['class_name'].lower())}\b", Path(path).name.lower()):
        score += 40

    for match in matches:
        line = clean_line(match.get("line", "")).lower()
        if not line:
            continue
        if query_lower and query_lower in line:
            score += 20
        if info["class_name"] and re.search(rf"\b({'|'.join(DEFINITION_KEYWORDS)})\s+{re.escape(info['class_name'].lower())}\b", line):
            score += 80
        if info["member_name"] and is_method_definition_line(line, info["member_name"].lower()):
            score += 70
        elif simple_symbol and is_method_definition_line(line, simple_symbol):
            score += 65
        elif info["class_name"] and info["member_name"] and re.search(
            rf"\b{re.escape(info['class_name'].lower())}\s*\.\s*{re.escape(info['member_name'].lower())}\b",
            line,
        ):
            score += 20
        elif simple_symbol and re.search(rf"\.\s*{re.escape(simple_symbol)}\s*\(", line):
            score += 18
        elif last_token and re.search(rf"\b{re.escape(last_token)}\s*\(", line):
            score += 15
        elif last_token and re.search(rf"\b{re.escape(last_token)}\b", line):
            score += 8
    return score


def should_include_result(path, matches, original_query):
    info = analyze_query(original_query)
    if not info["looks_like_fqn"]:
        return True

    lower_path = path.lower()
    query_lower = info["lower"]
    package_name = info["package_name"].lower()
    package_path = info["package_path"].lower()

    if package_path and package_path in lower_path:
        return True

    # For Class.member queries, the class's own file is exactly where the
    # member is defined; keep it even when no line spells out "Class.member".
    if info["class_name"] and info["member_name"] and path_matches_class_file(path, info["class_name"]):
        return True

    for match in matches:
        line = clean_line(match.get("line", "")).lower()
        if not line:
            continue
        if query_lower in line:
            return True
        if package_name and f"package {package_name}" in line:
            return True
        if f"import {query_lower}" in line:
            return True
        if info["class_name"] and info["member_name"]:
            if re.search(rf"\b{re.escape(info['class_name'].lower())}\s*\.\s*{re.escape(info['member_name'].lower())}\b", line):
                return True

    return False


def classify_match(line, info):
    lower = line.lower()
    simple_symbol = info["raw"].lower() if info["looks_like_simple_symbol"] else ""
    if info["class_name"] and re.search(rf"\b({'|'.join(DEFINITION_KEYWORDS)})\s+{re.escape(info['class_name'].lower())}\b", lower):
        return "class-definition"
    if info["member_name"] and is_method_definition_line(lower, info["member_name"].lower()):
        return "member-definition"
    if simple_symbol and is_method_definition_line(lower, simple_symbol):
        return "member-definition"
    if info["class_name"] and info["member_name"] and re.search(
        rf"\b{re.escape(info['class_name'].lower())}\s*\.\s*{re.escape(info['member_name'].lower())}\b",
        lower,
    ):
        return "member-reference"
    if simple_symbol and re.search(rf"\.\s*{re.escape(simple_symbol)}\s*\(", lower):
        return "member-reference"
    if info["package_name"] and f"package {info['package_name'].lower()}" in lower:
        return "package"
    if info["class_name"] and f"import {info['package_name'].lower()}.{info['class_name'].lower()}" in lower:
        return "class-reference"
    if info["class_name"] and re.search(rf"\b{re.escape(info['class_name'].lower())}\b", lower):
        return "class-reference"
    if info["member_name"] and re.search(rf"\b{re.escape(info['member_name'].lower())}\b", lower):
        return "member-reference"
    return "text"


def classify_result(path, matches, original_query):
    info = analyze_query(original_query)
    lower_path = path.lower()
    # Class.member: only the class's own file can rank as a definition.
    if info["class_name"] and info["member_name"]:
        if path_matches_class_file(path, info["class_name"]):
            if any(
                is_method_definition_line(clean_line(match.get("line", "")).lower(), info["member_name"])
                for match in matches
            ):
                return "definition"
            # The def-plan hit on the class file: this is where the member lives.
            return "definition" if matches else "member-reference"
        categories = [
            classify_match(clean_line(match.get("line", "")), info) for match in matches
        ]
        if "member-reference" in categories:
            return "member-reference"
        return "text"
    if info["class_name"] and path_matches_class_file(path, info["class_name"]):
        return "definition"

    categories = [classify_match(clean_line(match.get("line", "")), info) for match in matches]
    if "member-definition" in categories or "class-definition" in categories:
        return "definition"
    if info["class_name"] and path_matches_class_file(path, info["class_name"]):
        return "path"
    if any(category.endswith("reference") for category in categories):
        return "reference"
    return "text"


def fetch_search(field, query, projects, file_type, path_scope="", max_files=None):
    params = build_search_params(query, field, projects, file_type, path_scope, max_files)
    return json.loads(request("/api/v1/search", params))


def join_projects(projects):
    return ",".join(projects) if projects else None


def resolve_limit(limit):
    if limit is not None:
        return limit
    return load_config().get("default_limit", DEFAULT_LIMIT)


def print_results(result_items, total_time_ms, total_result_count, limit, project_scope_label=None, raw_counts=None):
    resolved_limit = resolve_limit(limit)
    print(f"time_ms: {total_time_ms}")
    print(f"result_count: {total_result_count}")
    if raw_counts is not None and raw_counts != total_result_count:
        print(f"# result_count counts deduplicated files; raw match lines across all index queries: {raw_counts}")
    print(f"returned_files: {min(resolved_limit, len(result_items))}")
    if project_scope_label:
        print(f"project_scope: {project_scope_label}")
    print()

    if not result_items:
        print("No results")
        return

    max_files = resolved_limit
    for item in result_items[:max_files]:
        project, display_path = split_project_path(item["path"])
        print(f"[{item['kind']}] {display_path}")
        if project:
            print(f"  project: {project}")
        for match in order_matches(item["matches"], item.get("query_hint")):
            line_no = match.get("lineNumber", "")
            line = clean_line(match.get("line", ""))
            if line_no:
                print(f"  {line_no}: {line}")
            else:
                print(f"  {line}")
        print()


def order_matches(matches, query_hint=None):
    """Order matches: query-term definition lines first, then exact query-term
    hits, then the rest; each group by line number; cap per file."""
    hint = (query_hint or "").lower().split(".")[-1]

    def is_def_line(line):
        return any(re.search(rf"\b{kw}\s+\w+", line) for kw in DEFINITION_KEYWORDS)

    def sort_key(match):
        line = strip_html_tags(match.get("line", "")).lower()
        exact = bool(hint and hint in line)
        try:
            line_no = int(match.get("lineNumber") or 0)
        except (TypeError, ValueError):
            line_no = 0
        if exact and is_def_line(line):
            rank = 0
        elif exact:
            rank = 1
        else:
            rank = 2
        return (rank, line_no)

    return sorted(matches, key=sort_key)[:MAX_MATCHES_PER_FILE]


def execute_plan(plan, query, projects, file_type, path_scope, mode=None, early_stop=False, max_files=None):
    """Run plan requests in parallel and aggregate per-file results.

    mode: 'def'/'symbol' filtering for targeted plans, None for smart plans.
    early_stop: skip low-priority requests once a definition hit is already
        aggregated with enough coverage (used by smart plans).
    max_files: per-request backend cap (maxresults).
    Returns {"items", "time_ms", "result_count"} where result_count is the
    number of aggregated files (deduplicated), plus "raw_counts" sum.
    """
    aggregated = {}
    total_raw_counts = 0

    def aggregate(plan_item, data):
        # Runs on the main thread only (futures are consumed via
        # as_completed below), so no locking is needed.
        nonlocal total_raw_counts
        total_raw_counts += data.get("resultCount") or 0
        for path, matches in (data.get("results") or {}).items():
            if not should_include_result(path, matches, query):
                continue
            kind = classify_result(path, matches, query)
            if mode == "def" and kind != "definition":
                continue
            if mode == "symbol" and kind == "definition":
                continue

            score = score_result(path, matches, query, plan_item)
            current = aggregated.get(path)
            if current is None:
                aggregated[path] = {
                    "path": path,
                    "matches": matches,
                    "score": score,
                    "kind": kind,
                    "query_hint": query.replace('"', "").strip(),
                }
                continue

            current["matches"] = merge_matches(current["matches"], matches)
            current["score"] = max(current["score"], score)
            current["kind"] = classify_result(path, current["matches"], query)

    def has_definition_coverage():
        return any(item["kind"] == "definition" for item in aggregated.values())

    def run_batch(batch):
        with ThreadPoolExecutor(max_workers=min(len(batch), MAX_PARALLEL_REQUESTS)) as pool:
            futures = {
                pool.submit(
                    fetch_search, item["field"], item["query"], projects, file_type, path_scope,
                    max_files,
                ): item
                for item in batch
            }
            # Consume in completion order (not submission order) so a single
            # slow request cannot delay aggregation of the ones already done.
            for future in as_completed(futures):
                item = futures[future]
                try:
                    data = future.result()
                except Exception as exc:  # one failing field must not kill the search
                    print(f"warning: {item['field']} search failed: {exc}", file=sys.stderr)
                    continue
                aggregate(item, data)

    # Primaries must run before secondaries, otherwise every plan item lands
    # in the first parallel batch and the early-stop check below never fires.
    ordered = (
        [item for item in plan if item.get("priority", "primary") == "primary"]
        + [item for item in plan if item.get("priority") == "secondary"]
    )
    wall_start = time.monotonic()
    remaining = ordered
    while remaining:
        batch, remaining = remaining[:MAX_PARALLEL_REQUESTS], remaining[MAX_PARALLEL_REQUESTS:]
        run_batch(batch)
        # Definition-first: once a definition is aggregated, reference-only
        # lookups (secondary) add little for the fuzzy-locate use case.
        if early_stop and remaining and has_definition_coverage():
            remaining = [item for item in remaining if item.get("priority") != "secondary"]
    return {
        "items": sorted(
            aggregated.values(),
            key=lambda item: (kind_rank(item["kind"]), -item["score"], item["path"]),
        ),
        # wall clock of the whole plan (parallel batches), not the sum of
        # per-request server times
        "time_ms": int((time.monotonic() - wall_start) * 1000),
        "result_count": len(aggregated),
        "raw_counts": total_raw_counts,
    }


def run_smart_search_plan(query, projects, file_type, path_scope="", limit=None):
    return execute_plan(
        build_smart_plan(query, file_type), query, projects, file_type, path_scope,
        mode=None, early_stop=True, max_files=plan_fetch_cap(limit),
    )


def run_targeted_search_plan(query, projects, file_type, mode, path_scope="", limit=None):
    info = analyze_query(query)
    plan = []

    def add(field, term, weight):
        if term:
            plan.append({"field": field, "query": term, "weight": weight, "category": mode})

    if mode == "def":
        if info["looks_like_member_query"]:
            add("path", info["normalized_symbol_path"], 220)
            add("path", info["class_name"], 210)
            add("full", f"\"{query}\"", 205)
            add("def", info["member_name"], 190)
            add("def", info["class_name"], 170)
        elif info["looks_like_class_query"]:
            add("path", info["normalized_symbol_path"], 210)
            add("path", info["class_name"], 200)
            add("def", info["class_name"], 190)
            add("full", f"\"{query}\"", 160)
        else:
            add("def", query, 180)
    elif mode == "symbol":
        if info["looks_like_member_query"]:
            add("symbol", info["member_name"], 180)
            add("full", f"\"{info['class_name']}.{info['member_name']}\"", 170)
            add("full", info["member_name"], 150)
        elif info["looks_like_class_query"]:
            add("symbol", info["class_name"], 170)
            add("full", f"\"{query}\"", 150)
        else:
            add("symbol", query, 160)
    else:
        raise ValueError(f"unsupported targeted search mode: {mode}")

    return execute_plan(plan, query, projects, file_type, path_scope, mode=mode, max_files=plan_fetch_cap(limit))


def run_plain_search_plan(query, projects, file_type, search_field, path_scope="", max_files=None):
    data = fetch_search(search_field, query, projects, file_type, path_scope, max_files)
    return {
        "items": [
            {"path": path, "matches": matches, "score": 0, "kind": classify_result(path, matches, query)}
            for path, matches in (data.get("results") or {}).items()
        ],
        "time_ms": data.get("time"),
        "result_count": data.get("resultCount"),
        "raw_counts": data.get("resultCount"),
    }


def smart_search(query, projects, file_type, limit, allow_global_fallback=False, path_scope=""):
    result, project_scope_label = run_search_with_fallback(
        lambda q, p, t: run_smart_search_plan(q, p, t, path_scope, limit),
        query,
        projects,
        file_type,
        allow_global_fallback=allow_global_fallback,
    )
    print_results(result["items"], result["time_ms"], result["result_count"], limit, project_scope_label=project_scope_label, raw_counts=result.get("raw_counts"))


def run_search_with_fallback(plan_runner, query, projects, file_type, allow_global_fallback=False):
    cfg = load_config()
    requested_projects = parse_projects(projects, [])
    scoped_projects = requested_projects or parse_projects(None, cfg.get("default_projects", []))
    scoped_projects_arg = join_projects(scoped_projects)

    result = plan_runner(query, scoped_projects_arg, file_type)
    project_scope_label = ",".join(scoped_projects) if scoped_projects else "all"

    info = analyze_query(query)
    can_fallback_to_all = not requested_projects and scoped_projects and (
        info["looks_like_fqn"] or looks_like_path(query) or info["looks_like_simple_symbol"] or allow_global_fallback
    )
    if not result["items"] and can_fallback_to_all:
        all_projects = list_projects_data()
        if all_projects and set(all_projects) != set(scoped_projects):
            result = plan_runner(query, join_projects(all_projects), file_type)
            project_scope_label = f"{','.join(scoped_projects)} -> all projects"

    return result, project_scope_label


def plain_search(query, search_field, projects, file_type, limit, allow_global_fallback=False, path_scope=""):
    if search_field in {"def", "symbol"}:
        result, project_scope_label = run_search_with_fallback(
            lambda q, p, t: run_targeted_search_plan(q, p, t, search_field, path_scope, limit),
            query,
            projects,
            file_type,
            allow_global_fallback=allow_global_fallback,
        )
        print_results(result["items"], result["time_ms"], result["result_count"], limit, project_scope_label=project_scope_label, raw_counts=result.get("raw_counts"))
        return

    result, project_scope_label = run_search_with_fallback(
        lambda q, p, t: run_plain_search_plan(q, p, t, search_field, path_scope, plain_fetch_cap(limit)),
        query,
        projects,
        file_type,
        allow_global_fallback=allow_global_fallback,
    )
    print_results(result["items"], result["time_ms"], result["result_count"], limit, project_scope_label=project_scope_label, raw_counts=result.get("raw_counts"))


def search(keywords, keyword_mode, search_field, projects, file_type, limit, path_scope=""):
    # Validate cheap arguments up front so bad input fails loudly instead of
    # being swallowed by the per-request error handling inside plan workers.
    if file_type:
        normalize_file_type(file_type)
    if path_scope and normalize_search_field(search_field) not in SEARCH_FIELDS:
        raise ValueError("unsupported search field")

    effective_query, is_multi_keyword = build_effective_query(keywords, keyword_mode)
    field = normalize_search_field(search_field)
    if is_multi_keyword and field == "smart":
        field = "full"
    # def/symbol are single-term index fields; an exact phrase only makes
    # sense as a full-text search.
    if field in {"def", "symbol"} and is_phrase(effective_query.strip().strip('"')):
        field = "full"

    if field == "smart":
        smart_search(
            effective_query, projects, file_type, limit,
            allow_global_fallback=is_multi_keyword, path_scope=path_scope,
        )
        return

    plain_search(
        effective_query, field, projects, file_type, limit,
        allow_global_fallback=is_multi_keyword, path_scope=path_scope,
    )


def clean_line(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value))


def strip_html_tags(value):
    return re.sub(r"<[^>]+>", "", value)


def kind_rank(kind):
    order = {
        "definition": 0,
        "path": 1,
        "reference": 2,
        "text": 3,
    }
    return order.get(kind, 99)


def path_matches_class_file(path, class_name):
    lower_path = path.lower()
    lower_class = class_name.lower()
    return any(lower_path.endswith(f"/{lower_class}{extension}") for extension in PATH_EXTENSIONS)


def split_project_path(path):
    normalized = (path or "").strip()
    if not normalized.startswith("/"):
        return None, normalized
    parts = normalized.split("/", 2)
    if len(parts) < 3:
        return None, normalized.lstrip("/")
    return parts[1], parts[2]


def is_method_definition_line(lower_line, member_name):
    if re.search(rf"\.\s*{re.escape(member_name)}\s*\(", lower_line):
        return False
    if re.search(rf"=\s*{re.escape(member_name)}\s*\(", lower_line):
        # assignment like `final Foo f = getService();`, not a definition
        return False
    if re.search(
        rf"\b(public|private|protected|internal|open|override|static|final|abstract|synchronized|native|suspend|fun)\b.*\b{re.escape(member_name)}\s*\(",
        lower_line,
    ):
        return True
    return bool(
        re.search(
            rf"^\s*(?:@[\w.]+\s+)*(?:[\w<>\[\],?]+\s+)+{re.escape(member_name)}\s*\(",
            lower_line,
        )
    )


def merge_matches(existing_matches, new_matches):
    merged = []
    seen = set()
    for match in existing_matches + new_matches:
        key = (match.get("lineNumber"), clean_line(match.get("line", "")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(match)
    return merged


def main():
    parser = argparse.ArgumentParser(description="rk_codesearch command")
    parser.add_argument("action")
    parser.add_argument("--keywords")
    parser.add_argument("--keyword-mode")
    parser.add_argument("--search-field")
    parser.add_argument("--project")
    parser.add_argument("--type")
    parser.add_argument("--path", help="restrict results to paths containing these /-separated terms, e.g. frameworks/base")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    if args.action == "list_projects":
        list_projects()
        return
    if args.action == "search":
        search(
            args.keywords,
            args.keyword_mode,
            args.search_field,
            args.project,
            args.type,
            args.limit,
            path_scope=args.path or "",
        )
        return

    raise ValueError(f"unsupported action: {args.action}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
