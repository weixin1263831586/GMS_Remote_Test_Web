"""OpenAPI owns operation schemas; curated metadata supplies business prose."""

import json
import re
import shlex
from urllib.parse import quote, urlencode

from features.system.api_docs_list import API_DOCS_LIST


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def resolve_schema(value, document: dict, seen: frozenset = frozenset()):
    """Resolve local references, retaining recursive references without looping."""
    if isinstance(value, list):
        return [resolve_schema(item, document, seen) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get("$ref")
    if reference and reference.startswith("#/") and reference not in seen:
        target = document
        for part in reference[2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~"), {})
        return resolve_schema(
            {**target, **{key: item for key, item in value.items() if key != "$ref"}},
            document, seen | {reference},
        )
    return {key: resolve_schema(item, document, seen) for key, item in value.items()}


def schema_example(schema: dict):
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    if schema.get("examples"):
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if schema.get(key):
            choice = next((item for item in schema[key] if item.get("type") != "null"), {})
            return schema_example(choice)
    if schema.get("allOf"):
        result = {}
        for part in schema["allOf"]:
            example = schema_example(part)
            if isinstance(example, dict):
                result.update(example)
        return result
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {
            name: schema_example(item)
            for name, item in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [schema_example(schema.get("items", {}))]
    if kind in {"integer", "number"}:
        return schema.get("minimum", 1)
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return "/path/to/file" if schema.get("format") == "binary" else "VALUE"


def _parameter(name: str, schema: dict, required: bool, location: str, description="") -> dict:
    kind = schema.get("type")
    if not kind:
        alternatives = schema.get("anyOf") or schema.get("oneOf") or []
        kind = next((part.get("type") for part in alternatives if part.get("type") != "null"), "object")
    if schema.get("format") == "binary" or (
        kind == "array" and schema.get("items", {}).get("format") == "binary"
    ):
        kind = "file"
    return {
        "name": name, "type": kind, "required": required, "in": location,
        "desc": description or schema.get("description", ""), "schema": schema,
        "example": schema_example(schema),
    }


def _media_content(content: dict, document: dict) -> dict:
    result = {}
    for media_type, media in content.items():
        schema = resolve_schema(media.get("schema", {}), document)
        example = media.get("example", schema_example(schema))
        if media.get("examples"):
            example = next(iter(media["examples"].values())).get("value", example)
        result[media_type] = {"schema": schema, "example": example}
    return result


def build_api_catalog(document: dict) -> list[dict]:
    curated = {(str(item["method"]).upper(), item["path"]): item for item in API_DOCS_LIST}
    result = []
    for path, path_item in sorted(document.get("paths", {}).items()):
        if not path.startswith("/api/"):
            continue
        for method, operation in sorted(path_item.items()):
            if method not in HTTP_METHODS:
                continue
            business = curated.get((method.upper(), path), {})
            descriptions = {
                item["name"]: item.get("desc", "")
                for item in business.get("params", []) if isinstance(item, dict)
            }
            parameters = []
            for item in path_item.get("parameters", []) + operation.get("parameters", []):
                item = resolve_schema(item, document)
                parameters.append(_parameter(
                    item["name"], item.get("schema", {}), bool(item.get("required")),
                    item["in"], item.get("description") or descriptions.get(item["name"], ""),
                ))
            body = resolve_schema(operation.get("requestBody", {}), document)
            content = _media_content(body.get("content", {}), document)
            content_type = next(iter(content), "")
            request_body = {
                "required": bool(body.get("required")),
                "description": body.get("description", ""), "content": content,
            } if body else None
            body_params = []
            if content:
                schema = content[content_type]["schema"]
                properties = schema.get("properties", {})
                location = "form" if content_type in {"multipart/form-data", "application/x-www-form-urlencoded"} else "body"
                body_params = [
                    _parameter(name, item, name in schema.get("required", []), location, descriptions.get(name, ""))
                    for name, item in properties.items()
                ] or [_parameter("body", schema, bool(body.get("required")), location, body.get("description", ""))]
            responses = {
                status: {
                    "description": response.get("description", ""),
                    "content": _media_content(response.get("content", {}), document),
                }
                for status, raw in operation.get("responses", {}).items()
                for response in [resolve_schema(raw, document)]
            }
            description = business.get("description") or operation.get("description") or operation.get("summary", "")
            entry = {
                "method": method.upper(), "path": path,
                "title": business.get("description") or operation.get("summary") or operation.get("operationId", path),
                "description": description, "parameters": parameters,
                "params": parameters + body_params, "request_body": request_body,
                "content_type": content_type, "responses": responses,
                "category": business.get("category") or (operation.get("tags") or ["other"])[0],
                "operation_id": operation.get("operationId", ""),
                "documented": bool(business), "source": "openapi",
            }
            if business.get("skill"):
                entry["skill"] = business["skill"]
            result.append(entry)
    return result


def curl_example(api: dict, base_url: str) -> str:
    path = api["path"]
    query = []
    headers = []
    for parameter in api["parameters"]:
        value = parameter["example"]
        if parameter["in"] == "path":
            path = path.replace("{" + parameter["name"] + "}", quote(str(value), safe=""))
        elif parameter["in"] == "query":
            query.append((parameter["name"], value))
        elif parameter["in"] == "header":
            headers += ["-H", f'{parameter["name"]}: {value}']
    path = re.sub(r"\{[^}]+\}", "VALUE", path)
    url = base_url.rstrip("/") + path + ("?" + urlencode(query, doseq=True) if query else "")
    args = ["curl", "-sS", "-X", api["method"], url, *headers]
    body = api.get("request_body")
    if body:
        media_type = api["content_type"]
        example = body["content"][media_type]["example"]
        if media_type in {"multipart/form-data", "application/x-www-form-urlencoded"}:
            for parameter in api["params"]:
                if parameter["in"] != "form":
                    continue
                value = "@/path/to/file" if parameter["type"] == "file" else str(parameter["example"])
                args += ["-F" if media_type == "multipart/form-data" else "--data-urlencode", f'{parameter["name"]}={value}']
        else:
            value = json.dumps(example, ensure_ascii=False) if "json" in media_type else str(example)
            args += ["-H", f"Content-Type: {media_type}", "--data-raw", value]
    return shlex.join(args)


def format_catalog_help(catalog: list[dict], base_url: str, api_path: str | None = None) -> str | None:
    if not api_path:
        return "GMS Auto Test API List\n\n" + f"Total: {len(catalog)} APIs\n\n" + "\n".join(
            f'{api["method"]:<10} {api["path"]}' for api in catalog
        ) + "\n"
    matches = [api for api in catalog if api["path"].lstrip("/") == api_path.lstrip("/")]
    if not matches:
        return None
    blocks = []
    for api in matches:
        lines = [f'{api["method"]} {api["path"]}', api["description"], "", "Parameters:"]
        for parameter in api["params"]:
            requirement = "required" if parameter["required"] else "optional"
            lines.append(f'{parameter["in"]}: {parameter["name"]} ({parameter["type"]}, {requirement}) {parameter["desc"]}')
        if api["content_type"]:
            lines.append(f'Content-Type: {api["content_type"]}')
        lines += ["", curl_example(api, base_url), "", "Responses:", json.dumps(api["responses"], ensure_ascii=False, indent=2)]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
