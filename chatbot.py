import json
import os

import streamlit as st
from langchain_openai import ChatOpenAI
from langchain.chains import LLMChain
from langchain_core.prompts import PromptTemplate
from langchain.memory import ConversationBufferWindowMemory

from dotenv import load_dotenv

from ai.attachment_analysis import (
    build_attachment_analysis_prompt,
    extract_attachment,
    format_extracted_content,
    list_mail_attachments,
    retrieve_relevant_attachment_chunks,
)
from ai.task_extraction import extract_tasks_from_email, is_task_extraction_request
from integrations.mail.fetch_mail import mailbox_dirs
from integrations.notion.mcp_client import NotionMcpClient
from integrations.notion.tasks import (
    STATUS_OPTIONS,
    check_task_duplicates,
    create_database,
    create_task,
    load_config as load_notion_config,
    query_tasks,
    update_task_status,
)
from services.ingest import embedding, ingest_mailbox

load_dotenv()


def render_notion_sidebar():
    config = load_notion_config()
    with st.sidebar:
        st.header("Notion 업무")
        if st.button("Notion 연결 확인", icon="🔗", key="notion_connect"):
            try:
                with st.spinner("Notion에 연결하고 있습니다..."):
                    result = NotionMcpClient().call_tool("notion-get-self", {})
                st.session_state.notion_message = (
                    "Notion 연결이 확인되었습니다."
                    if result
                    else "Notion 연결은 되었지만 계정 정보를 받지 못했습니다."
                )
                st.session_state.notion_message_is_error = False
            except Exception as error:
                st.session_state.notion_message = str(error)
                st.session_state.notion_message_is_error = True

        if st.session_state.get("notion_message"):
            if st.session_state.get("notion_message_is_error"):
                st.error(st.session_state.notion_message)
            else:
                st.success(st.session_state.notion_message)

        parent_page_id = st.text_input(
            "업무 DB를 만들 페이지 ID",
            value=config.get("parent_page_id")
            or os.getenv("NOTION_PARENT_PAGE_ID", ""),
            key="notion_parent_page_id",
        )

        if not config.get("data_source_id"):
            st.caption("업무 데이터베이스가 아직 연결되지 않았습니다.")
            if st.button("메일 업무 DB 생성", icon="➕", key="notion_create_database"):
                try:
                    with st.spinner("Notion 업무 DB를 생성하고 있습니다..."):
                        create_database(
                            NotionMcpClient(),
                            parent_page_id.strip(),
                        )
                    st.session_state.notion_message = "메일 업무 DB를 생성했습니다."
                    st.session_state.notion_message_is_error = False
                    st.rerun()
                except Exception as error:
                    st.error(f"Notion DB 생성에 실패했습니다: {error}")
            return

        st.caption("업무 데이터베이스 연결됨")
        if config.get("database_url"):
            st.link_button("Notion DB 열기", config["database_url"])
        if st.button("업무 조회", icon="🔎", key="notion_query_tasks"):
            try:
                with st.spinner("Notion 업무를 불러오는 중..."):
                    st.session_state.notion_task_rows = query_tasks(
                        NotionMcpClient(),
                        config["data_source_id"],
                    )
                st.session_state.notion_message = "Notion 업무를 조회했습니다."
                st.session_state.notion_message_is_error = False
            except Exception as error:
                st.error(f"Notion 업무 조회에 실패했습니다: {error}")

        task_rows = st.session_state.get("notion_task_rows", [])
        if task_rows:
            for task_row in task_rows:
                with st.expander(task_row.get("task_title") or "(제목 없음)"):
                    st.caption(
                        f"상태: {task_row.get('status') or '미착수'} · "
                        f"프로젝트: {task_row.get('project') or '미지정'}"
                    )
                    if task_row.get("url"):
                        st.link_button("Notion에서 열기", task_row["url"])

            selectable_rows = [row for row in task_rows if row.get("page_id")]
            if selectable_rows:
                page_ids = [row["page_id"] for row in selectable_rows]
                selected_page_id = st.selectbox(
                    "상태를 변경할 업무",
                    page_ids,
                    format_func=lambda page_id: next(
                        row.get("task_title") or page_id
                        for row in selectable_rows
                        if row["page_id"] == page_id
                    ),
                    key="notion_status_page",
                )
                new_status = st.selectbox(
                    "새 상태",
                    STATUS_OPTIONS,
                    key="notion_new_status",
                )
                if st.button("상태 변경", icon="↻", key="notion_update_status"):
                    try:
                        with st.spinner("Notion 상태를 변경하고 있습니다..."):
                            update_task_status(
                                NotionMcpClient(),
                                selected_page_id,
                                new_status,
                            )
                        st.session_state.notion_task_rows = [
                            {
                                **row,
                                "status": new_status,
                            }
                            if row.get("page_id") == selected_page_id
                            else row
                            for row in task_rows
                        ]
                        st.success("업무 상태를 변경했습니다.")
                    except Exception as error:
                        st.error(f"상태 변경에 실패했습니다: {error}")
        elif "notion_task_rows" in st.session_state:
            st.info("조회된 업무가 없습니다.")


st.title("그룹웨어 메일 챗봇")
render_notion_sidebar()

if "mailbox" not in st.session_state:
    st.session_state.mailbox = None

if st.session_state.mailbox is None:
    default_mailbox = os.getenv("GROUPWARE_MAILBOX", "INBOX")
    with st.form("mailbox_form"):
        mailbox_input = st.text_input("읽어올 메일함 이름을 입력하세요", value=default_mailbox)
        submitted = st.form_submit_button("불러오기")

    if submitted and mailbox_input.strip():
        try:
            with st.spinner(f"'{mailbox_input}' 메일함을 불러오는 중..."):
                vectordb = ingest_mailbox(mailbox_input.strip())
        except Exception as e:
            st.error(f"메일함을 불러오는 중 오류가 발생했습니다: {e}")
        else:
            st.session_state.retriever = vectordb.as_retriever(search_kwargs={"k": 4})
            st.session_state.mailbox = mailbox_input.strip()
            st.rerun()

    st.stop()

retriever = st.session_state.retriever
st.caption(f"현재 메일함: {st.session_state.mailbox}")

prompt = PromptTemplate(
    input_variables=["chat_history", "context", "question"],
    template="""You are an AI assistant that answers questions about the user's
    groupware mailbox. Answer the question using only the information in the
    [메일] below. If the answer isn't in the [메일], say you don't know.

    [메일]
    {context}

    chat_history: {chat_history},
    Human: {question}
    AI:"""
)

llm = ChatOpenAI(temperature=0, model_name="gpt-4.1")

if "memory" not in st.session_state:
    st.session_state.memory = ConversationBufferWindowMemory(
        memory_key="chat_history",
        input_key="question",
        k=5,
    )

llm_chain = LLMChain(
    llm=llm,
    memory=st.session_state.memory,
    prompt=prompt,
)


def load_task_sources(search_message):
    raw_dir, _ = mailbox_dirs(st.session_state.mailbox)
    sources = []
    seen_sources = set()

    for result in search_message.get("search_results", []):
        source_name = os.path.basename(result.get("source", ""))
        mail_uid = os.path.splitext(source_name)[0]
        if not mail_uid or mail_uid in seen_sources:
            continue
        seen_sources.add(mail_uid)

        try:
            mail_data = json.loads(
                (raw_dir / f"{mail_uid}.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue

        attachments = []
        for attachment in list_mail_attachments(st.session_state.mailbox, mail_uid):
            try:
                extraction = extract_attachment(attachment["path"])
                content = format_extracted_content(extraction)
            except Exception:
                content = ""
            if content:
                attachments.append({
                    "filename": attachment["filename"],
                    "content": content,
                })

        sources.append({
            "source_email": mail_data.get("message_id") or mail_uid,
            "title": mail_data.get("title", ""),
            "from": mail_data.get("from", ""),
            "to": mail_data.get("to", ""),
            "date": mail_data.get("date", ""),
            "content": mail_data.get("content", ""),
            "attachments": attachments,
        })
    return sources


def render_task_results(message, message_index):
    if not message.get("task_extraction"):
        return
    tasks = message.get("tasks", [])
    st.markdown("**메일에서 추출한 업무 JSON**")
    if not tasks:
        st.json([])
        return

    config = load_notion_config()
    for task_index, task in enumerate(tasks):
        st.json(task)
        duplicate_key = f"notion_duplicates_{message_index}_{task_index}"
        if st.button(
            "Notion 중복 확인",
            key=f"notion_check_duplicate_{message_index}_{task_index}",
            icon="🔎",
        ):
            try:
                if not config.get("data_source_id"):
                    raise ValueError("사이드바에서 먼저 Notion 업무 DB를 생성하세요.")
                with st.spinner("Notion에서 같은 업무를 확인하고 있습니다..."):
                    matches = check_task_duplicates(
                        NotionMcpClient(),
                        config["data_source_id"],
                        task,
                    )
                st.session_state[duplicate_key] = matches
            except Exception as error:
                st.error(f"중복 확인에 실패했습니다: {error}")

        if duplicate_key in st.session_state:
            duplicates = st.session_state[duplicate_key]
            if duplicates:
                st.warning("같은 제목의 업무가 이미 등록되어 있습니다.")
                for duplicate in duplicates:
                    title = duplicate.get("task_title") or "(제목 없음)"
                    if duplicate.get("url"):
                        st.markdown(f"- [{title}]({duplicate['url']})")
                    else:
                        st.markdown(f"- {title}")
            elif st.button(
                "Notion에 신규 업무 등록",
                key=f"notion_register_task_{message_index}_{task_index}",
                icon="➕",
            ):
                try:
                    with st.spinner("Notion에 업무를 등록하고 있습니다..."):
                        result = create_task(
                            NotionMcpClient(),
                            config["database_id"],
                            config["data_source_id"],
                            task,
                        )
                    if result["created"]:
                        st.success("Notion에 업무를 등록했습니다.")
                        st.session_state.pop(duplicate_key, None)
                    else:
                        st.session_state[duplicate_key] = result["duplicates"]
                        st.warning("등록 직전 중복 업무가 발견되어 등록을 중단했습니다.")
                except Exception as error:
                    st.error(f"Notion 업무 등록에 실패했습니다: {error}")


def render_search_results(message, search_id):
    results = message.get("search_results", [])
    if not results:
        return

    attachments_by_mail = {}
    for result in results:
        source_name = os.path.basename(result.get("source", ""))
        mail_uid = os.path.splitext(source_name)[0]
        attachments_by_mail[mail_uid] = list_mail_attachments(
            st.session_state.mailbox,
            mail_uid,
        )
    attachment_mail_count = sum(bool(items) for items in attachments_by_mail.values())
    guidance = (
        f"> 총 {len(results)}건의 메일을 찾았습니다.\n"
        f"> 첨부파일이 포함된 메일은 {attachment_mail_count}건입니다.\n"
        "> 메일 본문뿐 아니라 첨부파일의 상세 내용도 검색할 수 있습니다."
    )
    if attachment_mail_count:
        guidance += (
            "\n\n> 선택한 메일에 첨부된 파일의 내용을 검색할 수 있습니다.\n"
            "> 어떤 내용을 찾으시나요?"
        )
    st.markdown(guidance)

    with st.expander("참고한 메일"):
        for result in results:
            st.markdown(
                f"- **{result['title']}** — {result['from']} ({result['date']})"
            )
            source_name = os.path.basename(result.get("source", ""))
            mail_uid = os.path.splitext(source_name)[0]
            attachments = attachments_by_mail.get(mail_uid, [])
            if not attachments:
                continue

            st.caption("첨부파일: " + ", ".join(item["filename"] for item in attachments))


def render_attachment_results(message):
    chunks = message.get("attachment_chunks", [])
    if not chunks:
        return

    st.markdown(
        f"> 첨부파일에서 관련 내용을 {len(chunks)}건 찾았습니다.\n"
        "> 파일명과 페이지 정보를 함께 표시했습니다."
    )
    st.markdown("**첨부파일 분석 결과**")
    st.markdown(message["content"])
    for chunk in chunks:
        page_info = (
            f"{', '.join(str(page) for page in chunk['pages'])}페이지"
            if chunk["pages"]
            else "페이지 정보 없음"
        )
        st.caption(f"{chunk['filename']} · {page_info}")
        st.write(chunk["text"])

if "messages" not in st.session_state.keys():
    st.session_state.messages = [
        {"role": "assistant", "content": "안녕하세요, 메일함 내용에 대해 무엇이든 물어보세요."}
    ]

for message_index, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"]):
        if message.get("task_extraction"):
            render_task_results(message, message_index)
        elif message.get("attachment_chunks"):
            render_attachment_results(message)
        else:
            st.write(message["content"])
        render_search_results(message, message_index)

user_prompt = st.chat_input()

if user_prompt is not None:
    st.session_state.messages.append({"role": "user", "content": user_prompt})
    with st.chat_message("user"):
        st.write(user_prompt)

if st.session_state.messages[-1]["role"] != "assistant":
    recent_mail_search = next(
        (
            message
            for message in reversed(st.session_state.messages[:-1])
            if message.get("search_results")
        ),
        None,
    )
    previous_mail_search = (
        st.session_state.messages[-2]
        if len(st.session_state.messages) > 1
        and st.session_state.messages[-2].get("attachment_followup")
        else None
    )
    if is_task_extraction_request(user_prompt):
        with st.chat_message("assistant"):
            if not recent_mail_search:
                message_text = "먼저 업무를 추출할 메일을 검색해 주세요."
                st.info(message_text)
                new_ai_message = {
                    "role": "assistant",
                    "content": message_text,
                }
            else:
                try:
                    with st.spinner("메일과 첨부파일에서 업무 요청을 추출하고 있습니다..."):
                        sources = load_task_sources(recent_mail_search)
                        task_llm = ChatOpenAI(
                            temperature=0,
                            model_name="gpt-4.1-mini",
                            model_kwargs={
                                "response_format": {"type": "json_object"}
                            },
                        )
                        tasks = []
                        for source in sources:
                            tasks.extend(extract_tasks_from_email(task_llm, source))
                    new_ai_message = {
                        "role": "assistant",
                        "content": "메일에서 추출한 업무 JSON",
                        "task_extraction": True,
                        "tasks": tasks,
                    }
                    render_task_results(new_ai_message, len(st.session_state.messages))
                except Exception as error:
                    message_text = f"업무 추출에 실패했습니다: {error}"
                    st.error(message_text)
                    new_ai_message = {
                        "role": "assistant",
                        "content": message_text,
                    }
    elif previous_mail_search:
        attachments = []
        for result in previous_mail_search["search_results"]:
            source_name = os.path.basename(result.get("source", ""))
            mail_uid = os.path.splitext(source_name)[0]
            attachments.extend(list_mail_attachments(st.session_state.mailbox, mail_uid))

        with st.chat_message("assistant"):
            with st.spinner("선택한 메일의 첨부파일을 검색하고 있습니다..."):
                retrieval = retrieve_relevant_attachment_chunks(
                    attachments,
                    user_prompt,
                    embedding,
                    top_k=4,
                )
                chunks = retrieval["chunks"]
                answer = ""
                if chunks:
                    analysis_prompt = build_attachment_analysis_prompt(user_prompt, chunks)
                    response = ChatOpenAI(
                        temperature=0,
                        model_name="gpt-4.1-mini",
                    ).invoke(analysis_prompt)
                    answer = response.content
                    if not isinstance(answer, str):
                        answer = str(answer)

            if chunks:
                new_ai_message = {
                    "role": "assistant",
                    "content": answer,
                    "attachment_chunks": chunks,
                }
                render_attachment_results(new_ai_message)
            else:
                no_results = (
                    "해당 메일의 첨부파일에서 관련 내용을 찾지 못했습니다.\n\n"
                    "첨부파일이 정상적으로 분석되었는지 확인하거나 다른 키워드로 검색해 주세요."
                )
                st.markdown(no_results)
                new_ai_message = {"role": "assistant", "content": no_results}
    else:
        with st.chat_message("assistant"):
            with st.spinner("메일함 검색 중..."):
                docs = retriever.invoke(user_prompt)
                context = "\n\n".join(
                    f"제목: {doc.metadata.get('title', '')}\n"
                    f"보낸사람: {doc.metadata.get('from', '')}\n"
                    f"날짜: {doc.metadata.get('date', '')}\n"
                    f"내용: {doc.page_content}"
                    for doc in docs
                )
                ai_response = llm_chain.predict(question=user_prompt, context=context)
                st.write(ai_response)
                search_results = []
                seen_sources = set()
                for doc in docs:
                    source = doc.metadata.get("source", "")
                    if source in seen_sources:
                        continue
                    seen_sources.add(source)
                    search_results.append({
                        "source": source,
                        "title": doc.metadata.get("title") or "(제목 없음)",
                        "from": doc.metadata.get("from", ""),
                        "date": doc.metadata.get("date", ""),
                    })
                attachment_followup = any(
                    list_mail_attachments(
                        st.session_state.mailbox,
                        os.path.splitext(os.path.basename(result.get("source", "")))[0],
                    )
                    for result in search_results
                )
                new_ai_message = {
                    "role": "assistant",
                    "content": ai_response,
                    "search_query": user_prompt,
                    "search_results": search_results,
                    "attachment_followup": attachment_followup,
                }
                render_search_results(new_ai_message, len(st.session_state.messages))
    st.session_state.messages.append(new_ai_message)
