import json
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from config.paths import NOTION_CONFIG_PATH


CONFIG_PATH = NOTION_CONFIG_PATH
STATUS_OPTIONS = ("미착수", "진행 중", "완료", "보류")
PROPERTY_NAMES = {
    "task_title": "업무명",
    "project": "프로젝트",
    "task_type": "업무 유형",
    "description": "설명",
    "priority": "우선순위",
    "assignee": "담당자",
    "due_date": "마감일",
    "source_email": "원본 메일",
    "status": "상태",
}


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(config):
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def normalize_task_title(title):
    return re.sub(r"[^0-9a-z가-힣]+", "", str(title).casefold())


def find_duplicate_tasks(task, existing_tasks):
    title = normalize_task_title(task.get("task_title", ""))
    project = normalize_task_title(task.get("project") or "")
    if not title:
        return []

    duplicates = []
    for existing in existing_tasks:
        existing_title = normalize_task_title(existing.get("task_title", ""))
        existing_project = normalize_task_title(existing.get("project") or "")
        title_matches = title == existing_title
        project_matches = not project or not existing_project or project == existing_project
        if title_matches and project_matches:
            duplicates.append(existing)
    return duplicates


def task_properties(task, status="미착수"):
    values = {
        "task_title": task.get("task_title"),
        "project": task.get("project"),
        "task_type": task.get("task_type"),
        "description": task.get("description"),
        "priority": task.get("priority"),
        "assignee": task.get("assignee"),
        "due_date": task.get("due_date"),
        "source_email": task.get("source_email"),
        "status": status,
    }
    return {
        PROPERTY_NAMES[key]: value
        for key, value in values.items()
        if value is not None
    }


def database_properties():
    return {
        PROPERTY_NAMES["task_title"]: {"title": {}},
        PROPERTY_NAMES["project"]: {"rich_text": {}},
        PROPERTY_NAMES["task_type"]: {
            "select": {
                "options": [
                    {"name": option}
                    for option in ("기능 개선", "버그 수정", "신규 개발", "운영/지원", "문서화", "기타")
                ]
            }
        },
        PROPERTY_NAMES["description"]: {"rich_text": {}},
        PROPERTY_NAMES["priority"]: {
            "select": {"options": [{"name": option} for option in ("High", "Medium", "Low")]}
        },
        PROPERTY_NAMES["assignee"]: {"rich_text": {}},
        PROPERTY_NAMES["due_date"]: {"date": {}},
        PROPERTY_NAMES["source_email"]: {"rich_text": {}},
        PROPERTY_NAMES["status"]: {
            "status": {"options": [{"name": option} for option in STATUS_OPTIONS]}
        },
    }


def database_schema():
    task_types = "'기능 개선':blue, '버그 수정':red, '신규 개발':green, '운영/지원':yellow, '문서화':purple, '기타':gray"
    priorities = "'High':red, 'Medium':yellow, 'Low':green"
    statuses = (
        "'미착수':gray:to_do, '진행 중':blue:in_progress, "
        "'완료':green:complete, '보류':yellow:to_do"
    )
    return (
        'CREATE TABLE ('
        '"업무명" TITLE, '
        '"프로젝트" RICH_TEXT, '
        f'"업무 유형" SELECT({task_types}), '
        '"설명" RICH_TEXT, '
        f'"우선순위" SELECT({priorities}), '
        '"담당자" RICH_TEXT, '
        '"마감일" DATE, '
        '"원본 메일" RICH_TEXT, '
        f'"상태" STATUS({statuses})'
        ')'
    )


def task_page_properties(task, status="미착수"):
    values = {
        "업무명": task.get("task_title"),
        "프로젝트": task.get("project"),
        "업무 유형": task.get("task_type"),
        "설명": task.get("description"),
        "우선순위": task.get("priority"),
        "담당자": task.get("assignee"),
        "원본 메일": task.get("source_email"),
        "상태": status,
    }
    properties = {key: value for key, value in values.items() if value is not None}
    if task.get("due_date"):
        properties[f"date:{PROPERTY_NAMES['due_date']}:start"] = task["due_date"]
    return properties


def _read_property(row, property_name, fallback_property_name=None):
    value = row.get(property_name)
    if value is None and fallback_property_name:
        value = row.get(fallback_property_name)
    if isinstance(value, dict):
        for key in ("title", "rich_text", "select", "status", "date", "number", "people"):
            nested = value.get(key)
            if isinstance(nested, list):
                return "".join(item.get("plain_text", "") for item in nested)
            if isinstance(nested, dict):
                return nested.get("name") or nested.get("start") or ""
            if nested is not None:
                return nested
    return value


def normalize_task_row(row):
    normalized = {
        key: _read_property(
            row,
            property_name,
            f"date:{property_name}:start" if key == "due_date" else None,
        )
        for key, property_name in PROPERTY_NAMES.items()
    }
    page_url = row.get("url")
    normalized["page_id"] = (
        row.get("id")
        or row.get("page_id")
        or (urlparse(page_url).path.rstrip("/").split("/")[-1] if page_url else None)
    )
    normalized["url"] = row.get("url")
    return normalized


def _response_payload(response):
    if isinstance(response, dict):
        return response
    if isinstance(response, list):
        return response
    return {"result": response}


def _tool_payload(response):
    payload = _response_payload(response)
    if "results" in payload or "id" in payload or "data_sources" in payload:
        return payload
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return payload


def _call_tool(client, tool_name, arguments):
    return _tool_payload(client.call_tool(tool_name, arguments))


def _view_url_from_database(client, database_id, database_url=None):
    response = _call_tool(client, "notion-fetch", {"id": database_id})
    text = response.get("text", "")
    view_match = re.search(r'<view\s+url="\{\{view://([^}]+)\}\}">', text)
    if not view_match:
        return None

    database_url = response.get("url") or database_url
    if not database_url:
        return None
    parsed_url = urlparse(database_url)
    query = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
    query["v"] = view_match.group(1)
    return urlunparse(
        (parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", urlencode(query), "")
    )


def _database_ids_from_parent(client, parent_page_id, title):
    response = _call_tool(client, "notion-fetch", {"id": parent_page_id})
    text = response.get("text", "")
    database_entries = re.findall(
        r'<database\s+url="([^"]+)"\s+data-source-url="collection://([^"]+)"[^>]*>(.*?)</database>',
        text,
        flags=re.DOTALL,
    )
    matches = [
        (database_url, data_source_id, database_content)
        for database_url, data_source_id, database_content in database_entries
        if re.search(rf"(?:^|>)\s*{re.escape(title)}\s*(?:<|$)", database_content)
    ]
    if len(matches) != 1:
        return None
    database_url, data_source_id, database_content = matches[0]
    database_id = urlparse(database_url).path.rstrip("/").split("/")[-1]
    view_match = re.search(r'<view\s+url="\{\{view://([^}]+)\}\}">', database_content)
    if not view_match:
        return None
    parsed_url = urlparse(database_url)
    query = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
    query["v"] = view_match.group(1)
    view_url = urlunparse(
        (parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", urlencode(query), "")
    )
    return database_id, data_source_id, database_url, view_url


def attach_existing_database(client, parent_page_id, title="메일 업무"):
    database_ids = _database_ids_from_parent(client, parent_page_id, title)
    if database_ids is None:
        raise ValueError(f"부모 페이지에서 '{title}' 데이터베이스를 유일하게 찾지 못했습니다.")
    database_id, data_source_id, database_url, view_url = database_ids
    config = load_config()
    config.update({
        "parent_page_id": parent_page_id,
        "database_id": database_id,
        "data_source_id": data_source_id,
        "database_url": database_url,
        "view_url": view_url,
        "properties": PROPERTY_NAMES,
    })
    save_config(config)
    return config


def create_database(client, parent_page_id, title="메일 업무"):
    response = _call_tool(
        client,
        "notion-create-database",
        {
            "parent": {"page_id": parent_page_id},
            "title": title,
            "schema": database_schema(),
        },
    )
    data_sources = response.get("data_sources", [])
    database = response.get("database", {})
    data_source = response.get("data_source", {})
    data_source_id = (
        data_sources[0].get("id")
        if data_sources
        else response.get("data_source_id") or data_source.get("id")
    )
    database_id = response.get("database_id") or response.get("id") or database.get("id")
    database_url = response.get("url") or database.get("url")
    recovered = _database_ids_from_parent(client, parent_page_id, title)
    if recovered is None and (not database_id or not data_source_id):
        raise ValueError("Notion DB 생성 후 database view 정보를 찾지 못했습니다.")
    if recovered:
        recovered_database_id, recovered_data_source_id, recovered_url, view_url = recovered
        database_id = database_id or recovered_database_id
        data_source_id = data_source_id or recovered_data_source_id
        database_url = database_url or recovered_url
    else:
        view_url = None
    config = load_config()
    config.update({
        "parent_page_id": parent_page_id,
        "data_source_id": data_source_id,
        "database_id": database_id,
        "database_url": database_url,
        "view_url": view_url,
        "properties": PROPERTY_NAMES,
    })
    save_config(config)
    return response


def query_tasks(client, view_url, limit=100):
    if not view_url:
        config = load_config()
        view_url = _view_url_from_database(
            client,
            config.get("database_id"),
            config.get("database_url"),
        )
        if not view_url:
            raise ValueError("Notion DB의 기본 view URL을 찾지 못했습니다.")
        config["view_url"] = view_url
        save_config(config)

    response = _call_tool(
        client,
        "notion-query-data-sources",
        {
            "data": {
                "mode": "view",
                "view_url": view_url,
                "page_size": min(max(int(limit), 1), 100),
            },
        },
    )
    rows = response.get("results", [])
    return [normalize_task_row(row) for row in rows]


def check_task_duplicates(client, view_url, task):
    return find_duplicate_tasks(task, query_tasks(client, view_url))


def create_task(client, database_id, view_url, task):
    duplicates = check_task_duplicates(client, view_url, task)
    if duplicates:
        return {"created": False, "duplicates": duplicates}

    result = _call_tool(
        client,
        "notion-create-pages",
        {
            "parent": {"database_id": database_id},
            "pages": [{"properties": task_page_properties(task)}],
            "allow_async": False,
        },
    )
    return {"created": True, "result": result}


def update_task_status(client, page_id, status):
    if status not in STATUS_OPTIONS:
        raise ValueError(f"지원하지 않는 상태입니다: {status}")
    return _call_tool(
        client,
        "notion-update-page",
        {
            "page_id": page_id,
            "command": "update_properties",
            "properties": {PROPERTY_NAMES["status"]: status},
            "allow_async": False,
        },
    )