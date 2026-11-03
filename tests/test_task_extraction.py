import json
import unittest

from ai.task_extraction import (
    build_task_extraction_prompt,
    extract_tasks_from_email,
    is_task_extraction_request,
    parse_task_extraction_response,
)


class TaskExtractionTests(unittest.TestCase):
    def test_build_prompt_includes_email_and_attachment_context(self):
        prompt = build_task_extraction_prompt({
            "title": "부서 검색 개선 요청",
            "content": "부서명으로 검색 기능을 추가해 주세요.",
            "attachments": [{
                "filename": "요구사항.txt",
                "content": "부서 코드 검색도 지원",
            }],
        })

        self.assertIn("부서명으로 검색 기능을 추가해 주세요.", prompt)
        self.assertIn("부서 코드 검색도 지원", prompt)
        self.assertIn("tasks를 빈 배열", prompt)

    def test_detects_task_extraction_commands(self):
        self.assertTrue(is_task_extraction_request("메일 업무를 JSON으로 추출해줘"))
        self.assertTrue(is_task_extraction_request("요청사항 정리해줘"))
        self.assertFalse(is_task_extraction_request("첨부파일에서 일정 검색해줘"))

    def test_parser_returns_requested_fields_and_trusted_source_id(self):
        response = {
            "tasks": [{
                "task_title": "부서 검색 기능 추가",
                "project": "그룹웨어 고도화",
                "task_type": "기능 개선",
                "description": "부서명과 부서 코드 검색 지원",
                "priority": "High",
                "assignee": None,
                "due_date": None,
                "source_email": "model-invented-id",
            }]
        }

        tasks = parse_task_extraction_response(
            json.dumps(response, ensure_ascii=False),
            "stored-message-id",
        )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(
            list(tasks[0]),
            [
                "task_title",
                "project",
                "task_type",
                "description",
                "priority",
                "assignee",
                "due_date",
                "source_email",
            ],
        )
        self.assertEqual(tasks[0]["source_email"], "stored-message-id")
        self.assertEqual(tasks[0]["priority"], "High")

    def test_mail_recipients_override_model_assignee(self):
        response = {
            "tasks": [{
                "task_title": "부서 검색 기능 추가",
                "assignee": "모델이 추측한 담당자",
            }]
        }

        tasks = parse_task_extraction_response(
            response,
            "mail-id",
            "홍길동 <hong@example.com>; lee@example.com",
        )

        self.assertEqual(
            tasks[0]["assignee"],
            "홍길동 <hong@example.com>; lee@example.com",
        )

    def test_missing_mail_recipients_produce_null_assignee(self):
        tasks = parse_task_extraction_response(
            {"tasks": [{"task_title": "업무", "assignee": "추측 담당자"}]},
            "mail-id",
        )

        self.assertIsNone(tasks[0]["assignee"])

    def test_parser_skips_empty_tasks_and_ignores_extra_fields(self):
        response = {
            "tasks": [
                {"task_title": ""},
                {
                    "task_title": "업무",
                    "priority": "Urgent",
                    "due_date": "다음 주 금요일",
                    "confidence": 1.4,
                },
            ]
        }

        tasks = parse_task_extraction_response(response, "mail-id")

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["priority"], "Medium")
        self.assertIsNone(tasks[0]["due_date"])
        self.assertNotIn("confidence", tasks[0])

    def test_extract_tasks_calls_model_and_returns_empty_when_no_tasks(self):
        class FakeLLM:
            def invoke(self, prompt):
                self.prompt = prompt
                return type("Response", (), {"content": '{"tasks": []}'})()

        llm = FakeLLM()
        tasks = extract_tasks_from_email(
            llm,
            {"source_email": "mail-id", "content": "감사합니다.", "attachments": []},
        )

        self.assertEqual(tasks, [])
        self.assertIn("감사합니다.", llm.prompt)


if __name__ == "__main__":
    unittest.main()