# 그룹웨어 메일 챗봇

그룹웨어 메일을 IMAP으로 가져와 벡터 DB로 색인한 뒤, Streamlit 채팅 화면에서 메일 내용을 질문하고 업무를 추출해 Notion에 등록하는 애플리케이션입니다.

## 주요 기능

- **메일 검색 챗봇**: 메일 본문을 검색해 답변하고, 참고한 메일(제목·발신자·날짜)을 함께 보여줍니다
- **첨부파일 분석**: PDF, DOCX, XLSX, PPTX, CSV, TXT 첨부파일 내용을 검색합니다
- **업무 추출**: 검색한 메일과 첨부파일에서 업무를 JSON으로 뽑아냅니다
- **Notion 연동**: Notion Remote MCP로 업무 DB를 만들고, 중복을 확인한 뒤 업무를 등록하거나 상태를 바꿉니다

## 기술 스택

- UI: Streamlit
- LLM: OpenAI `gpt-4.1`
- 임베딩: `intfloat/multilingual-e5-base`
- 벡터 DB: Chroma
- 연동: IMAP, Notion Remote MCP (OAuth)

## 빠른 시작

Python 3.10 이상이 필요합니다(Notion MCP SDK 요구사항).

```bash
python3.13 -m venv .venv-mcp
source .venv-mcp/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 실제 값으로 수정
streamlit run chatbot.py
```

브라우저에서 `http://localhost:8501`을 열고 메일함을 불러오면 됩니다.

### 환경 변수

| 변수 | 설명 | 기본값 |
|---|---|---|
| `GROUPWARE_IMAP_HOST` | IMAP 서버 주소 | |
| `GROUPWARE_IMAP_PORT` | IMAP 포트 | `993` |
| `GROUPWARE_EMAIL` / `GROUPWARE_PASSWORD` | 메일 계정 | |
| `GROUPWARE_MAILBOX` | 기본 메일함 | `INBOX` |
| `OPENAI_API_KEY` | OpenAI API 키 | |
| `NOTION_PARENT_PAGE_ID` | 업무 DB를 만들 Notion 페이지 ID (DB ID는 안 됨) | |

## 프로젝트 구조

```text
ai/                    첨부파일 분석 및 업무 추출
config/                공통 경로와 설정
data/                  메일 원본, 첨부파일, Chroma 인덱스 (Git 제외)
integrations/mail/     IMAP 메일 수집
integrations/notion/   Notion Remote MCP 클라이언트와 업무 저장소
services/              메일 수집·임베딩·인덱싱 서비스
tests/                 단위 테스트
chatbot.py             Streamlit 실행 진입점
```

## 보안 참고

메일, 첨부파일, 인덱스는 `data/` 아래 로컬에만 저장되고 Git에서는 제외됩니다. 답변을 만들거나 업무를 추출할 때는 검색된 메일 내용이 OpenAI API로 전송됩니다.

## 문서

자세한 사용법, 처리 흐름, 제약 사항, 문제 해결은 [MANUAL.md](MANUAL.md)를 참고하세요.
