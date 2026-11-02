import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from openpyxl import Workbook

from attachment_analysis import (
    build_attachment_analysis_prompt,
    extract_attachment,
    format_extracted_content,
    list_mail_attachments,
    retrieve_relevant_attachment_chunks,
    retrieve_relevant_chunks,
)


class AttachmentExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_extracts_docx_paragraph_and_table(self):
        path = self.root / "report.docx"
        document = Document()
        document.add_paragraph("Quarterly report")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Department"
        table.cell(0, 1).text = "Budget"
        document.save(path)

        result = extract_attachment(path)

        self.assertEqual(result["metadata"]["detected_type"], "DOCX")
        self.assertIn("Quarterly report", result["text"])
        self.assertEqual(result["tables"][0]["rows"][0], ["Department", "Budget"])

    def test_extracts_xlsx_sheet_as_table(self):
        path = self.root / "costs.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Costs"
        sheet.append(["Item", "Amount"])
        sheet.append(["Travel", 120])
        workbook.save(path)

        result = extract_attachment(path)

        self.assertEqual(result["metadata"]["detected_type"], "XLSX")
        self.assertEqual(result["tables"][0]["title"], "Costs")
        self.assertEqual(result["tables"][0]["rows"][1], ["Travel", "120"])
        self.assertIn("Travel\t120", format_extracted_content(result))

    def test_extracts_csv_rows_and_metadata(self):
        path = self.root / "records.csv"
        path.write_text("name,amount\nAlice,42\n", encoding="utf-8")

        result = extract_attachment(path)

        self.assertEqual(result["metadata"]["detected_type"], "CSV")
        self.assertEqual(result["tables"][0]["rows"][1], ["Alice", "42"])
        self.assertEqual(len(result["metadata"]["sha256"]), 64)

    def test_unsupported_file_still_returns_metadata(self):
        path = self.root / "archive.bin"
        path.write_bytes(b"not a supported document")

        result = extract_attachment(path)

        self.assertEqual(result["metadata"]["detected_type"], "UNKNOWN")
        self.assertFalse(result["metadata"]["supported"])
        self.assertTrue(result["warnings"])

    def test_oversized_file_skips_hash_and_content_extraction(self):
        path = self.root / "large.txt"
        with path.open("wb") as file:
            file.truncate(25 * 1024 * 1024 + 1)

        result = extract_attachment(path)

        self.assertEqual(result["metadata"]["sha256"], "")
        self.assertEqual(result["text"], "")
        self.assertTrue(result["warnings"])

    def test_attachment_listing_stays_inside_mail_uid_directory(self):
        raw_dir = self.root / "raw"
        attachment_root = self.root / "attachments"
        mail_dir = attachment_root / "42"
        raw_dir.mkdir()
        mail_dir.mkdir(parents=True)
        (mail_dir / "safe.txt").write_text("local content", encoding="utf-8")
        (attachment_root / "outside.txt").write_text("outside", encoding="utf-8")
        (raw_dir / "42.json").write_text(
            json.dumps({"title": "Test", "attachments": ["safe.txt", "../outside.txt"]}),
            encoding="utf-8",
        )
        (raw_dir / "43.json").write_text(
            json.dumps({"title": "Other", "attachments": ["outside.txt"]}),
            encoding="utf-8",
        )

        with patch("attachment_analysis.mailbox_dirs", return_value=(raw_dir, attachment_root)):
            attachments = list_mail_attachments("INBOX", mail_uid="42")

        self.assertEqual([item["filename"] for item in attachments], ["safe.txt"])

    def test_only_question_relevant_chunks_are_selected_for_prompt(self):
        class FakeEmbeddings:
            def embed_documents(self, texts):
                return [[1.0, 0.0] if "Renewal" in text else [0.0, 1.0] for text in texts]

            def embed_query(self, query):
                return [1.0, 0.0]

        content = "Renewal deadline is June 30.\n\n" + "Hardware inventory record. " * 30
        chunks = retrieve_relevant_chunks(
            content,
            "When is the renewal deadline?",
            FakeEmbeddings(),
            top_k=1,
        )
        chunks[0].update({"filename": "contract.txt", "detected_type": "TXT"})
        prompt = build_attachment_analysis_prompt(
            "When is the renewal deadline?",
            chunks,
        )

        self.assertEqual(len(chunks), 1)
        self.assertIn("Renewal deadline", prompt)
        self.assertNotIn("Hardware inventory", prompt)

    def test_attachment_retrieval_only_uses_attachments_from_selected_mail(self):
        class FakeEmbeddings:
            def embed_documents(self, texts):
                return [[1.0, 0.0] for _ in texts]

            def embed_query(self, query):
                return [1.0, 0.0]

        first_path = self.root / "first.txt"
        other_path = self.root / "other.txt"
        first_path.write_text("The renewal date is June 30.", encoding="utf-8")
        other_path.write_text("Unrelated second mail attachment.", encoding="utf-8")

        result = retrieve_relevant_attachment_chunks(
            [{"path": first_path}],
            "When is the renewal date?",
            FakeEmbeddings(),
        )

        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["filename"], "first.txt")
        self.assertTrue(result["chunks"])
        self.assertTrue(all(chunk["filename"] == "first.txt" for chunk in result["chunks"]))


if __name__ == "__main__":
    unittest.main()