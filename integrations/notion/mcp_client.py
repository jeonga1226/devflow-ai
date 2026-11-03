import asyncio
import importlib
import json
import queue
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


NOTION_MCP_SERVER = "https://mcp.notion.com"
NOTION_MCP_ENDPOINT = f"{NOTION_MCP_SERVER}/mcp"
OAUTH_CALLBACK_HOST = "127.0.0.1"
OAUTH_CALLBACK_PORT = 8765
KEYRING_SERVICE = "groupware-mail-chatbot-notion-mcp"
KEYRING_ACCOUNT = "oauth"


class KeyringTokenStorage:
    def __init__(self):
        try:
            self.keyring = importlib.import_module("keyring")
        except ImportError as error:
            raise RuntimeError(
                "Notion MCP에는 keyring과 Python 3.10 이상이 필요합니다. "
                "requirements.txt 설치 후 Python 환경을 전환하세요."
            ) from error

    def _read_json(self, key):
        value = self.keyring.get_password(KEYRING_SERVICE, f"{KEYRING_ACCOUNT}:{key}")
        return json.loads(value) if value else None

    def _write_json(self, key, value):
        serialized = value.model_dump_json(by_alias=True)
        self.keyring.set_password(
            KEYRING_SERVICE,
            f"{KEYRING_ACCOUNT}:{key}",
            serialized,
        )

    async def get_tokens(self):
        value = self._read_json("tokens")
        if value is None:
            return None
        token_type = importlib.import_module("mcp.shared.auth").OAuthToken
        return token_type.model_validate(value)

    async def set_tokens(self, tokens):
        self._write_json("tokens", tokens)

    async def get_client_info(self):
        value = self._read_json("client_info")
        if value is None:
            return None
        client_type = importlib.import_module(
            "mcp.shared.auth"
        ).OAuthClientInformationFull
        return client_type.model_validate(value)

    async def set_client_info(self, client_info):
        self._write_json("client_info", client_info)

    def clear(self):
        for key in ("tokens", "client_info"):
            try:
                self.keyring.delete_password(
                    KEYRING_SERVICE,
                    f"{KEYRING_ACCOUNT}:{key}",
                )
            except self.keyring.errors.PasswordDeleteError:
                pass


class OAuthCallbackServer:
    def __init__(self):
        self.results = queue.Queue()
        callback_results = self.results

        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse_qs(urlparse(self.path).query)
                callback_results.put({
                    "code": params.get("code", [None])[0],
                    "state": params.get("state", [None])[0],
                    "iss": params.get("iss", [None])[0],
                    "error": params.get("error", [None])[0],
                })
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "<p>Notion 연결을 완료했습니다. 이 탭을 닫아도 됩니다.</p>".encode()
                )

            def log_message(self, format_string, *args):
                return

        self.server = ThreadingHTTPServer(
            (OAUTH_CALLBACK_HOST, OAUTH_CALLBACK_PORT),
            CallbackHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def wait_for_result(self):
        result = self.results.get(timeout=300)
        if result["error"]:
            raise RuntimeError(f"Notion OAuth 승인 실패: {result['error']}")
        if not result["code"]:
            raise RuntimeError("Notion OAuth callback에 authorization code가 없습니다.")
        return result

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class NotionMcpClient:
    def __init__(self, timeout_seconds=60):
        self.timeout_seconds = timeout_seconds
        self.token_storage = KeyringTokenStorage()

    def call_tool(self, name, arguments):
        try:
            return asyncio.run(self._call_tool(name, arguments))
        except Exception as error:
            details = self._format_exception(error)
            raise RuntimeError(f"Notion MCP 요청 실패: {details}") from error

    def close(self):
        self.token_storage.clear()

    async def _call_tool(self, name, arguments):
        try:
            import httpx
            from mcp import ClientSession
            from mcp.client.auth import OAuthClientProvider
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared.auth import OAuthClientMetadata
        except ImportError as error:
            raise RuntimeError(
                "Notion Remote MCP 실행에는 Python 3.10 이상과 "
                "requirements.txt의 MCP 의존성이 필요합니다."
            ) from error

        callback = OAuthCallbackServer()
        callback.start()

        async def open_authorization_url(url):
            webbrowser.open(url)

        async def receive_callback():
            result = await asyncio.to_thread(callback.wait_for_result)
            return result["code"], result["state"]

        oauth = OAuthClientProvider(
            server_url=NOTION_MCP_ENDPOINT,
            client_metadata=OAuthClientMetadata(
                client_name="Groupware Mail Chatbot",
                token_endpoint_auth_method="none",
                redirect_uris=[
                    f"http://{OAUTH_CALLBACK_HOST}:{OAUTH_CALLBACK_PORT}/callback"
                ],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            ),
            storage=self.token_storage,
            redirect_handler=open_authorization_url,
            callback_handler=receive_callback,
        )

        try:
            timeout = httpx.Timeout(self.timeout_seconds, read=300.0)
            async with httpx.AsyncClient(auth=oauth, timeout=timeout) as http_client:
                async with streamable_http_client(
                    NOTION_MCP_ENDPOINT,
                    http_client=http_client,
                ) as (read_stream, write_stream, _get_session_id):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(name, arguments)
                        return self._unpack_tool_result(result)
        finally:
            callback.close()

    @staticmethod
    def _format_exception(error):
        nested_errors = getattr(error, "exceptions", None)
        if nested_errors:
            details = "; ".join(
                NotionMcpClient._format_exception(nested)
                for nested in nested_errors
            )
        else:
            details = f"{type(error).__name__}: {error}"
        return re.sub(
            r"(?i)(access_token|refresh_token|client_secret|authorization_code)=([^&\s]+)",
            r"\1=[redacted]",
            details,
        )

    @staticmethod
    def _unpack_tool_result(result):
        if getattr(result, "isError", False):
            details = "\n".join(
                item.text
                for item in getattr(result, "content", [])
                if getattr(item, "type", None) == "text"
            )
            raise RuntimeError(details or "Notion MCP tool call failed.")

        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured

        text_blocks = [
            item.text
            for item in getattr(result, "content", [])
            if getattr(item, "type", None) == "text"
        ]
        if not text_blocks:
            return {}
        content = "\n".join(text_blocks)
        try:
            return json.loads(content)
        except ValueError:
            return {"text": content}