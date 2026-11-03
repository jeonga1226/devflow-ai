import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from integrations.notion.tasks import (
    STATUS_OPTIONS,
    attach_existing_database,
    create_database,
    database_schema,
    database_properties,
    find_duplicate_tasks,
    normalize_task_row,
    create_task,
    task_page_properties,
    task_properties,
    update_task_status,
)


class NotionTaskMappingTests(unittest.TestCase):
    def test_builds_database_schema_with_required_fields(self):
        properties = database_properties()

        self.assertEqual(properties["업무명"], {"title": {}})
        self.assertEqual(properties["마감일"], {"date": {}})
        self.assertEqual(
            [option["name"] for option in properties["상태"]["status"]["options"]],
            list(STATUS_OPTIONS),
        )
        ddl = database_schema()
        self.assertIn('"업무명" TITLE', ddl)
        self.assertIn('"상태" STATUS(', ddl)
        self.assertNotIn('"신뢰도"', ddl)

    def test_maps_extracted_task_to_notion_properties(self):
        properties = task_properties({
            "task_title": "부서 검색 기능 추가",
            "project": "그룹웨어 고도화",
            "task_type": "기능 개선",
            "description": "부서 코드로 검색",
            "priority": "Medium",
            "assignee": None,
            "due_date": None,
            "source_email": "mail-id",
        })

        self.assertEqual(properties["업무명"], "부서 검색 기능 추가")
        self.assertEqual(properties["상태"], "미착수")
        self.assertNotIn("담당자", properties)
        self.assertEqual(properties["원본 메일"], "mail-id")
        self.assertNotIn("신뢰도", properties)

    def test_formats_page_date_property_for_notion_mcp(self):
        properties = task_page_properties({
            "task_title": "업무",
            "due_date": "2026-12-01",
        })

        self.assertEqual(properties["date:마감일:start"], "2026-12-01")
        self.assertNotIn("마감일", properties)

    def test_duplicate_check_normalizes_spacing_and_punctuation(self):
        task = {"task_title": "사용자 관리 화면 부서 검색 기능 추가"}
        existing = [
            {"task_title": "사용자 관리 화면, 부서 검색 기능 추가", "page_id": "page-1"},
            {"task_title": "사용자 권한 수정", "page_id": "page-2"},
        ]

        duplicates = find_duplicate_tasks(task, existing)

        self.assertEqual([row["page_id"] for row in duplicates], ["page-1"])

    def test_normalizes_notion_rows(self):
        task = normalize_task_row({
            "id": "page-1",
            "url": "https://notion.so/page-1",
            "업무명": {"title": [{"plain_text": "부서 검색"}]},
            "우선순위": {"select": {"name": "High"}},
            "마감일": {"date": {"start": "2026-12-01"}},
        })

        self.assertEqual(task["task_title"], "부서 검색")
        self.assertEqual(task["priority"], "High")
        self.assertEqual(task["due_date"], "2026-12-01")
        self.assertEqual(task["page_id"], "page-1")

    def test_normalizes_view_rows_with_page_url_and_expanded_date(self):
        task = normalize_task_row({
            "업무명": "부서 검색",
            "상태": "미착수",
            "date:마감일:start": "2026-12-01",
            "url": "https://app.notion.com/p/page-1",
        })

        self.assertEqual(task["task_title"], "부서 검색")
        self.assertEqual(task["due_date"], "2026-12-01")
        self.assertEqual(task["page_id"], "page-1")

    def test_rejects_unknown_status_before_calling_mcp(self):
        class FakeClient:
            def call_tool(self, name, arguments):
                raise AssertionError("MCP should not be called for invalid status")

        with self.assertRaises(ValueError):
            update_task_status(FakeClient(), "page-1", "완료됨")

    def test_create_task_checks_duplicates_then_uses_database_parent(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "notion-query-data-sources":
                    return {"results": []}
                return {"results": [{"id": "new-page"}]}

        client = FakeClient()
        result = create_task(
            client,
            "database-id",
            "https://notion.so/database-id?v=view-id",
            {"task_title": "새 업무", "source_email": "mail-id"},
        )

        self.assertTrue(result["created"])
        self.assertEqual(client.calls[0][0], "notion-query-data-sources")
        self.assertEqual(
            client.calls[0][1]["data"]["mode"],
            "view",
        )
        self.assertEqual(
            client.calls[0][1]["data"]["view_url"],
            "https://notion.so/database-id?v=view-id",
        )
        self.assertEqual(client.calls[1][0], "notion-create-pages")
        self.assertEqual(
            client.calls[1][1]["parent"],
            {"database_id": "database-id"},
        )

    def test_create_task_does_not_create_duplicate(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, arguments):
                self.calls.append(name)
                return {
                    "results": [{
                        "id": "existing-page",
                        "업무명": "이미 있는 업무",
                    }]
                }

        client = FakeClient()
        result = create_task(
            client,
            "database-id",
            "https://notion.so/database-id?v=view-id",
            {"task_title": "이미 있는 업무"},
        )

        self.assertFalse(result["created"])
        self.assertEqual(client.calls, ["notion-query-data-sources"])

    def test_create_database_persists_nested_database_and_data_source_ids(self):
        class FakeClient:
            def call_tool(self, name, arguments):
                if name == "notion-create-database":
                    self.arguments = arguments
                return {
                    "database": {
                        "id": "database-id",
                        "url": "https://notion.so/database-id",
                    },
                    "data_source": {"id": "data-source-id"},
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / ".notion_config.json"
            client = FakeClient()
            with patch("integrations.notion.tasks.CONFIG_PATH", config_path):
                response = create_database(client, "parent-id")
                saved_config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertIn("database", response)
        self.assertEqual(saved_config["database_id"], "database-id")
        self.assertEqual(saved_config["data_source_id"], "data-source-id")
        self.assertEqual(client.arguments["parent"], {"page_id": "parent-id"})
        self.assertIn('"업무명" TITLE', client.arguments["schema"])

    def test_attaches_existing_database_found_on_parent_page(self):
        class FakeClient:
            def call_tool(self, name, arguments):
                self.name = name
                return {
                    "text": (
                        '<database url="https://app.notion.com/p/database-1" '
                        'data-source-url="collection://source-1" inline="false">'
                        '메일 업무<views><view url="{{view://view-1}}">Default</view></views>'
                        "</database>"
                    )
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / ".notion_config.json"
            with patch("integrations.notion.tasks.CONFIG_PATH", config_path):
                config = attach_existing_database(FakeClient(), "parent-1")

        self.assertEqual(config["database_id"], "database-1")
        self.assertEqual(config["data_source_id"], "source-1")
        self.assertEqual(
            config["view_url"],
            "https://app.notion.com/p/database-1?v=view-1",
        )


if __name__ == "__main__":
    unittest.main()