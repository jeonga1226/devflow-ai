import json
import tempfile
import unittest
from pathlib import Path

from integrations.mail.fetch_mail import _update_existing_mail_recipient, format_recipients


class MailRecipientTests(unittest.TestCase):
    def test_formats_multiple_and_mime_encoded_recipients(self):
        recipients = format_recipients(
            '=?utf-8?Q?=ED=99=8D=EA=B8=B8=EB=8F=99?= <hong@example.com>, Lee <lee@example.com>'
        )

        self.assertEqual(
            recipients,
            "홍길동; Lee",
        )

    def test_backfills_recipient_for_existing_mail_without_overwriting_existing_value(self):
        class FakeImapConnection:
            def __init__(self):
                self.fetch_count = 0

            def uid(self, command, uid, query):
                self.fetch_count += 1
                self.asserted_query = query
                return "OK", [(b"1 (BODY[HEADER.FIELDS (TO)] {30}", b"To: Pat <pat@example.com>\r\n\r\n")]

        with tempfile.TemporaryDirectory() as temp_dir:
            mail_path = Path(temp_dir) / "1.json"
            mail_path.write_text(
                json.dumps({"title": "Existing mail"}),
                encoding="utf-8",
            )
            connection = FakeImapConnection()

            _update_existing_mail_recipient(connection, b"1", mail_path)
            updated = json.loads(mail_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["to"], "Pat")
            self.assertIn("HEADER.FIELDS (TO)", connection.asserted_query)

            _update_existing_mail_recipient(connection, b"1", mail_path)
            self.assertEqual(connection.fetch_count, 1)

    def test_normalizes_existing_recipient_emails_to_names(self):
        class FakeImapConnection:
            def uid(self, command, uid, query):
                raise AssertionError("Existing recipient names should be normalized locally")

        with tempfile.TemporaryDirectory() as temp_dir:
            mail_path = Path(temp_dir) / "1.json"
            mail_path.write_text(
                json.dumps({"title": "Existing mail", "to": "Pat <pat@example.com>"}),
                encoding="utf-8",
            )

            _update_existing_mail_recipient(FakeImapConnection(), b"1", mail_path)

            updated = json.loads(mail_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["to"], "Pat")

    def test_omits_email_only_recipients_without_display_names(self):
        self.assertEqual(format_recipients("pat@example.com"), "")


if __name__ == "__main__":
    unittest.main()