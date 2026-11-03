import os

import streamlit as st
from langchain_openai import ChatOpenAI
from langchain.chains import LLMChain
from langchain_core.prompts import PromptTemplate
from langchain.memory import ConversationBufferWindowMemory

from dotenv import load_dotenv

from ingest import embedding, ingest_mailbox
from attachment_analysis import (
    build_attachment_analysis_prompt,
    list_mail_attachments,
    retrieve_relevant_attachment_chunks,
)

load_dotenv()

st.title("그룹웨어 메일 챗봇")

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
        if message.get("attachment_chunks"):
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
    previous_mail_search = (
        st.session_state.messages[-2]
        if len(st.session_state.messages) > 1
        and st.session_state.messages[-2].get("attachment_followup")
        else None
    )
    if previous_mail_search:
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
