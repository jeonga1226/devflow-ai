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

    analyses = st.session_state.setdefault("mail_attachment_analyses", {})
    with st.expander("참고한 메일"):
        for result in results:
            st.markdown(
                f"- **{result['title']}** — {result['from']} ({result['date']})"
            )
            source_name = os.path.basename(result.get("source", ""))
            mail_uid = os.path.splitext(source_name)[0]
            attachments = list_mail_attachments(st.session_state.mailbox, mail_uid)
            if not attachments:
                continue

            st.caption("첨부파일: " + ", ".join(item["filename"] for item in attachments))
            analysis_key = f"{search_id}:{mail_uid}"
            if st.button(
                "이 메일의 첨부파일 검색 요청",
                key=f"attachment_search_{search_id}_{mail_uid}",
            ):
                try:
                    with st.spinner("이 메일의 첨부파일에서 질문 관련 내용을 찾고 있습니다..."):
                        retrieval = retrieve_relevant_attachment_chunks(
                            attachments,
                            message["search_query"],
                            embedding,
                            top_k=4,
                        )
                        answer = None
                        if retrieval["chunks"]:
                            analysis_prompt = build_attachment_analysis_prompt(
                                message["search_query"],
                                retrieval["chunks"],
                            )
                            response = ChatOpenAI(
                                temperature=0,
                                model_name="gpt-4.1-mini",
                            ).invoke(analysis_prompt)
                            answer = response.content
                            if not isinstance(answer, str):
                                answer = str(answer)
                        analyses[analysis_key] = {
                            "files": retrieval["files"],
                            "chunks": retrieval["chunks"],
                            "answer": answer,
                        }
                except Exception as error:
                    st.error(f"첨부파일 분석 요청에 실패했습니다: {error}")

            analysis = analyses.get(analysis_key)
            if analysis:
                st.markdown("**첨부파일 메타데이터**")
                st.dataframe(
                    [
                        {
                            "파일명": file["filename"],
                            "유형": file["detected_type"],
                            "크기(bytes)": file["size_bytes"],
                        }
                        for file in analysis["files"]
                    ],
                    hide_index=True,
                    use_container_width=True,
                )
                if analysis["chunks"]:
                    st.markdown("**GPT에 전달된 관련 첨부 청크**")
                    for chunk in analysis["chunks"]:
                        st.caption(
                            f"{chunk['filename']} · 코사인 유사도 {chunk['score']:.3f}"
                        )
                        st.write(chunk["text"])
                    st.markdown("**첨부파일 분석 결과**")
                    st.markdown(analysis["answer"])
                else:
                    st.warning("지원되는 내용이나 질문과 관련된 청크를 찾지 못했습니다.")

if "messages" not in st.session_state.keys():
    st.session_state.messages = [
        {"role": "assistant", "content": "안녕하세요, 메일함 내용에 대해 무엇이든 물어보세요."}
    ]

for message_index, message in enumerate(st.session_state.messages):
    with st.chat_message(message["role"]):
        st.write(message["content"])
        render_search_results(message, message_index)

user_prompt = st.chat_input()

if user_prompt is not None:
    st.session_state.messages.append({"role": "user", "content": user_prompt})
    with st.chat_message("user"):
        st.write(user_prompt)

if st.session_state.messages[-1]["role"] != "assistant":
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
            new_ai_message = {
                "role": "assistant",
                "content": ai_response,
                "search_query": user_prompt,
                "search_results": search_results,
            }
            search_id = len(st.session_state.messages)
            render_search_results(new_ai_message, search_id)
    st.session_state.messages.append(new_ai_message)
