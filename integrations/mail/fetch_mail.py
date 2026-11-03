# fetch_mail.py
"""IMAP으로 그룹웨어 메일함에 접속해 메일을 data/raw/ 하위에 JSON으로 저장"""
import base64
import email
import imaplib
import json
import os
import re

from email.header import decode_header
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv
from config.paths import ATTACHMENTS_ROOT, MAIL_DATA_ROOT

load_dotenv()

IMAP_HOST = os.getenv("GROUPWARE_IMAP_HOST")
IMAP_PORT = int(os.getenv("GROUPWARE_IMAP_PORT", "993"))
IMAP_USER = os.getenv("GROUPWARE_EMAIL")
IMAP_PASSWORD = os.getenv("GROUPWARE_PASSWORD")
MAILBOX = os.getenv("GROUPWARE_MAILBOX", "INBOX")


def sanitize_mailbox(mailbox):
    """메일함 이름을 폴더명으로 써도 안전하도록 정리"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", mailbox) or "INBOX"


def mailbox_dirs(mailbox):
    """메일함별로 분리된 raw/attachments 저장 경로를 반환"""
    safe_name = sanitize_mailbox(mailbox)
    return (
        MAIL_DATA_ROOT / safe_name,
        ATTACHMENTS_ROOT / safe_name,
    )


def imap_utf7_encode(mailbox):
    """IMAP4 메일함 이름 인코딩 규격(RFC 3501 Modified UTF-7)으로 변환.
    imaplib이 명령어를 ASCII로만 인코딩해 한글 등이 포함된 메일함 이름을
    그대로 넘기면 UnicodeEncodeError가 나므로, SELECT 전에 이 함수로 변환한다."""
    if "&" not in mailbox and all(0x20 <= ord(ch) <= 0x7E for ch in mailbox):
        return mailbox

    result = []
    buffer = ""

    def flush():
        nonlocal buffer
        if buffer:
            b64 = base64.b64encode(buffer.encode("utf-16-be")).decode("ascii")
            result.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buffer = ""

    for ch in mailbox:
        code = ord(ch)
        if 0x20 <= code <= 0x7E and ch != "&":
            flush()
            result.append(ch)
        elif ch == "&":
            flush()
            result.append("&-")
        else:
            buffer += ch
    flush()
    return "".join(result)


def decode_mime_words(value):
    """제목/발신자 등 MIME 인코딩된 헤더를 사람이 읽을 수 있는 문자열로 변환"""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def strip_html(html):
    """HTML 본문에서 태그를 제거하고 텍스트만 추출"""
    text = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", "", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def extract_body(msg):
    """메일에서 첨부파일을 제외한 본문 텍스트를 추출 (text/plain 우선, 없으면 text/html)"""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition:
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                payload = None
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if content_type == "text/plain" and not plain:
                plain = text
            elif content_type == "text/html" and not html:
                html = text
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if msg.get_content_type() == "text/html":
            html = text
        else:
            plain = text

    return plain.strip() if plain else strip_html(html)


def clean_whitespace(text):
    """연속된 개행과 탭 덩어리를 정리해 읽기 좋은 본문으로 변환"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\t+", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def save_attachments(msg, uid, attachments_dir):
    """메일의 첨부파일을 data/attachments/{mailbox}/{uid}/ 밑에 저장하고 파일명 목록을 반환"""
    if not msg.is_multipart():
        return []

    filenames = []
    mail_dir = attachments_dir / uid.decode()
    for part in msg.walk():
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" not in disposition:
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_mime_words(filename)
        filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", filename).strip(" .")
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue

        mail_dir.mkdir(parents=True, exist_ok=True)
        (mail_dir / filename).write_bytes(payload)
        filenames.append(filename)

    return filenames


def parse_message_id(raw, uid):
    """Message-ID 헤더에서 꺾쇠(<>)와 도메인을 제거해 로컬 파트만 추출"""
    local = raw.strip().strip("<>").split("@")[0] if raw else uid.decode()
    return re.sub(r"[^a-zA-Z0-9_-]", "_", local)


def fetch_and_save_mails(mailbox=None, limit=None):
    """메일함의 메일을 가져와 data/raw/{mailbox}/에 JSON으로 저장 (이미 저장된 메일은 건너뜀)"""
    mailbox = mailbox or MAILBOX
    output_dir, attachments_dir = mailbox_dirs(mailbox)
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(IMAP_USER, IMAP_PASSWORD)
    status, _ = conn.select(f'"{imap_utf7_encode(mailbox)}"')
    if status != "OK":
        conn.logout()
        raise RuntimeError(f"메일함 '{mailbox}'을(를) 열 수 없습니다: {status}")

    status, data = conn.uid("search", None, "ALL")
    if status != "OK":
        raise RuntimeError(f"메일 검색 실패: {status}")

    uids = data[0].split()
    if limit:
        uids = uids[-limit:]

    saved = 0
    for uid in uids:
        filepath = output_dir / f"{uid.decode()}.json"
        if filepath.exists():
            continue

        status, msg_data = conn.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK" or not msg_data:
            continue

        raw_message = next(
            (part[1] for part in msg_data
             if isinstance(part, tuple) and isinstance(part[1], (bytes, bytearray))),
            None,
        )
        if raw_message is None:
            continue

        msg = email.message_from_bytes(raw_message)
        message_id = parse_message_id(msg.get("Message-ID", ""), uid)

        subject = decode_mime_words(msg.get("Subject", ""))
        sender = decode_mime_words(msg.get("From", ""))
        try:
            date = parsedate_to_datetime(msg.get("Date", "")).isoformat()
        except (TypeError, ValueError):
            date = msg.get("Date", "")

        content = clean_whitespace(extract_body(msg))
        if not content:
            continue

        attachments = save_attachments(msg, uid, attachments_dir)

        filepath.write_text(
            json.dumps(
                {
                    "title": subject,
                    "from": sender,
                    "date": date,
                    "message_id": message_id,
                    "attachments": attachments,
                    "content": content,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        saved += 1

    conn.logout()
    return saved, len(uids)


if __name__ == "__main__":
    saved, total = fetch_and_save_mails()
    output_dir, _ = mailbox_dirs(MAILBOX)
    print(f"전체 {total}개 메일 중 {saved}개 신규 저장 완료: {output_dir}")
