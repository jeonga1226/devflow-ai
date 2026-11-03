import json
import re
from urllib.parse import urlparse

from config.paths import NOTION_CONFIG_PATH


CONFIG_PATH = NOTION_CONFIG_PATH
DATA_SOURCE_URL_PREFIX = "collection://"
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


def _read_property(row, property_name):
    value = row.get(property_name)
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
        key: _read_property(row, property_name)
        for key, property_name in PROPERTY_NAMES.items()
    }
    normalized["page_id"] = row.get("id") or row.get("page_id")
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


def _database_ids_from_parent(client, parent_page_id, title):
    response = _call_tool(client, "notion-fetch", {"id": parent_page_id})
    text = response.get("text", "")
    database_entries = re.findall(
        r'<database\s+url="([^"]+)"\s+data-source-url="collection://([^"]+)"[^>]*>\s*(.*?)\s*</database>',
        text,
        flags=re.DOTALL,
    )
    matches = [
        (database_url, data_source_id, database_title.strip())
        for database_url, data_source_id, database_title in database_entries
        if database_title.strip() == title
    ]
    if len(matches) != 1:
        return None
    database_url, data_source_id, _ = matches[0]
    database_id = urlparse(database_url).path.rstrip("/").split("/")[-1]
    return database_id, data_source_id, database_url


def attach_existing_database(client, parent_page_id, title="메일 업무"):
    database_ids = _database_ids_from_parent(client, parent_page_id, title)
    if database_ids is None:
        raise ValueError(f"부모 페이지에서 '{title}' 데이터베이스를 유일하게 찾지 못했습니다.")
    database_id, data_source_id, database_url = database_ids
    config = load_config()
    config.update({
        "parent_page_id": parent_page_id,
        "database_id": database_id,
        "data_source_id": data_source_id,
        "database_url": database_url,
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
    if not data_source_id:
        recovered = _database_ids_from_parent(client, parent_page_id, title)
        if recovered is None:
            raise ValueError("Notion DB 생성 후 부모 페이지에서 database ID를 찾지 못했습니다.")
        recovered_database_id, data_source_id, recovered_url = recovered
        database_id = database_id or recovered_database_id
        database_url = database_url or recovered_url
    config = load_config()
    config.update({
        "parent_page_id": parent_page_id,
        "data_source_id": data_source_id,
        "database_id": database_id,
        "database_url": database_url,
        "properties": PROPERTY_NAMES,
    })
    save_config(config)
    return response


def query_tasks(client, data_source_id, limit=100):
    response = _call_tool(
        client,
        "notion-query-data-sources",
        {
            "data": {
                "mode": "rows",
                "data_source_url": f"{DATA_SOURCE_URL_PREFIX}{data_source_id}",
                "limit": min(max(int(limit), 1), 100),
            },
        },
    )
    rows = response.get("results", [])
    return [normalize_task_row(row) for row in rows]


def check_task_duplicates(client, data_source_id, task):
    return find_duplicate_tasks(task, query_tasks(client, data_source_id))


def create_task(client, database_id, data_source_id, task):
    duplicates = check_task_duplicates(client, data_source_id, task)
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