# ingest.py
import json
import shutil

from chromadb.api.client import SharedSystemClient
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma

from config.paths import CHROMA_ROOT
from integrations.mail.fetch_mail import fetch_and_save_mails, mailbox_dirs, sanitize_mailbox, MAILBOX

splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=80,
)

embedding = HuggingFaceEmbeddings(model_name="intfloat/multilingual-e5-base")


def persist_dir_for(mailbox):
    """메일함별로 분리된 ChromaDB 저장 경로를 반환"""
    return str(CHROMA_ROOT / sanitize_mailbox(mailbox))


def load_documents(data_dir):
    """data/raw 하위 메일 JSON 파일을 읽어 Document로 변환"""
    documents = []
    for file in data_dir.glob("*.json"):
        data = json.loads(file.read_text(encoding="utf-8"))
        documents.append(Document(
            page_content=data["content"],
            metadata={
                "title": data["title"],
                "from": data["from"],
                "date": data["date"],
                "message_id": data["message_id"],
                "source": file.name,
            },
        ))
    return documents


def chunk_documents(documents):
    """Document 리스트를 청크 단위로 분할"""
    return splitter.split_documents(documents)


def build_vectordb(chunks, persist_dir):
    """청크를 임베딩하여 ChromaDB에 저장"""
    # 같은 프로세스(Streamlit)에서 다른 persist_directory로 클라이언트를 새로 만들면
    # chromadb가 이전 경로의 클라이언트를 캐시해뒀다가 readonly 오류를 내므로 캐시를 비워준다.
    SharedSystemClient.clear_system_cache()
    return Chroma.from_documents(
        documents=chunks,
        embedding=embedding,
        persist_directory=persist_dir,
    )


def ingest_mailbox(mailbox):
    """메일함 하나를 fetch하고 해당 메일함 전용 벡터DB를 빌드/갱신"""
    saved, total = fetch_and_save_mails(mailbox=mailbox)
    print(f"[{mailbox}] 전체 {total}개 메일 중 {saved}개 신규 저장 완료")

    data_dir, _ = mailbox_dirs(mailbox)
    documents = load_documents(data_dir)
    print(f"{len(documents)}개 메일 로드")

    chunks = chunk_documents(documents)
    print(f"{len(chunks)}개 청크 생성")

    if not chunks:
        raise ValueError(f"'{mailbox}' 메일함에서 읽을 메일을 찾지 못했습니다. 메일함 이름을 확인해주세요.")

    persist_dir = persist_dir_for(mailbox)
    shutil.rmtree(persist_dir, ignore_errors=True)  # 재실행 시 청크 중복 누적 방지
    vectordb = build_vectordb(chunks, persist_dir)
    print(f"벡터DB 저장 완료: {persist_dir}")
    return vectordb


def main():
    ingest_mailbox(MAILBOX)


if __name__ == "__main__":
    main()
