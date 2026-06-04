#!/usr/bin/env python3
import os
import sys
import json
import asyncio
import hmac
import hashlib
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
import pyodbc
import mcp.server.stdio
from mcp.server import Server
from mcp.types import Tool, TextContent

load_dotenv()

# api_key 驗證為 optional：
#   - 若 .env 有設定 API_KEY → 工具呼叫時需要帶入 api_key 並驗證
#   - 若未設定 API_KEY（stdio 本機模式）→ 跳過驗證，直接執行
API_KEY    = os.getenv("API_KEY")
JWT_SECRET = os.getenv("JWT_SECRET")
HTTP_PORT  = int(os.getenv("HTTP_PORT", 3000))
USE_HTTP   = "--http" in sys.argv

if USE_HTTP and not JWT_SECRET:
    print("ERROR: JWT_SECRET not set in .env", file=sys.stderr)
    sys.exit(1)
if USE_HTTP and not API_KEY:
    print("WARNING: API_KEY not set, HTTP mode will have no tool-level auth", file=sys.stderr)

CONN_STR = (
    f"DRIVER={{{os.getenv('DB_DRIVER', 'ODBC Driver 18 for SQL Server')}}};"
    f"SERVER={os.getenv('DB_SERVER')},{os.getenv('DB_PORT', '1433')};"
    f"DATABASE={os.getenv('DB_DATABASE')};"
    f"UID={os.getenv('DB_USER')};"
    f"PWD={os.getenv('DB_PASSWORD')};"
    "TrustServerCertificate=yes;"
    "Encrypt=no;"
)

# ── DB helper ─────────────────────────────────────────────
def run_query_sync(sql: str) -> list[dict]:
    """同步執行 query，回傳 list of dict。由 asyncio.to_thread 呼叫。"""
    with pyodbc.connect(CONN_STR, timeout=10) as conn:
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute(sql)
        cols = [col[0] for col in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

async def run_query(sql: str) -> list[dict]:
    return await asyncio.to_thread(run_query_sync, sql)

def friendly_db_error(e: Exception) -> str:
    """把常見的 DB 錯誤轉成對使用者友善的說明和修復建議。"""
    msg = str(e)
    if "Data source name not found" in msg or "找不到資料來源名稱" in msg or "IM002" in msg:
        return (
            "DB Error: ODBC 驅動程式未安裝或名稱錯誤。\n"
            "修復步驟：\n"
            "1. 確認已安裝正確的 ODBC 驅動，執行：python -c \"import pyodbc; print(pyodbc.drivers())\"\n"
            "2. 將輸出的驅動名稱填入 .env 的 DB_DRIVER\n"
            "3. 若清單為空，請至 https://learn.microsoft.com/zh-tw/sql/connect/odbc/download-odbc-driver-for-sql-server 下載安裝"
        )
    if "Login failed" in msg or "18456" in msg:
        return (
            "DB Error: 資料庫登入失敗。\n"
            "修復步驟：請確認 .env 的 DB_USER 和 DB_PASSWORD 是否正確。"
        )
    if "Network address" in msg or "TCP Provider" in msg or "10061" in msg or "連線被拒" in msg:
        return (
            "DB Error: 無法連線到資料庫伺服器。\n"
            "修復步驟：\n"
            "1. 確認 DB_SERVER 和 DB_PORT 設定正確\n"
            "2. 確認資料庫伺服器正在執行且網路可達\n"
            "3. 確認防火牆沒有封鎖該 port"
        )
    if "Cannot open database" in msg or "4060" in msg:
        return (
            f"DB Error: 資料庫不存在或無權限存取。\n"
            "修復步驟：請確認 .env 的 DB_DATABASE 名稱正確，且帳號有存取權限。"
        )
    return f"DB Error: {msg}"
# ─────────────────────────────────────────────────────────

# ── 安全驗證 ──────────────────────────────────────────────
def validate_query(query: str) -> tuple[bool, str]:
    normalized = query.strip().lower()

    if not normalized.startswith("select"):
        return False, "Only SELECT queries are allowed"

    blocked = [
        "insert", "update", "delete", "drop", "truncate", "alter", "create",
        "exec", "execute", "xp_", "sp_",
        "openrowset", "opendatasource", "bulk",
        "into outfile", "load_file",
        ";",
    ]
    for keyword in blocked:
        if keyword in normalized:
            return False, f"Blocked keyword: {keyword}"

    if "top " not in normalized and "where" not in normalized:
        return False, "Query must include TOP or WHERE to limit result size"

    return True, ""

def check_api_key(arguments: dict) -> tuple[bool, str]:
    """若 API_KEY 有設定則驗證，否則直接通過。"""
    if not API_KEY:
        return True, ""
    provided = arguments.get("api_key", "")
    if provided != API_KEY:
        return False, "Error: Invalid API key"
    return True, ""
# ─────────────────────────────────────────────────────────

# ── JWT 驗證（HTTP 模式用）────────────────────────────────
def verify_jwt(auth_header: str) -> bool:
    if not auth_header or not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]
    parts = token.split(".")
    if len(parts) != 3:
        return False
    header_b64, payload_b64, sig = parts
    expected = hmac.new(
        JWT_SECRET.encode(),
        f"{header_b64}.{payload_b64}".encode(),
        hashlib.sha256
    ).digest()
    expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
    if sig != expected_b64:
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        import time
        if "exp" in payload and payload["exp"] < time.time():
            return False
    except Exception:
        return False
    return True
# ─────────────────────────────────────────────────────────

app = Server("mssql-readonly")

@app.list_tools()
async def list_tools() -> list[Tool]:
    # api_key 完全從 schema 移除：
    # - 模型看不到這個參數，不會問使用者要 key
    # - 若 .env 有設定 API_KEY，可透過呼叫端（如 my-agent 的 inject）自動帶入
    # - call_tool 的 check_api_key() 仍會驗證（若 API_KEY 有設定的話）
    return [
        Tool(
            name="query",
            description=(
                "Execute a read-only SQL SELECT query against the MSSQL database. "
                "IMPORTANT RULES:\n"
                "- Only SELECT statements are allowed\n"
                "- Query MUST include TOP or WHERE clause to limit results (e.g. SELECT TOP 100 * FROM tablename)\n"
                "- To preview data in a table: SELECT TOP 100 * FROM dbo.tablename\n"
                "- To filter: SELECT TOP 100 * FROM dbo.tablename WHERE column = 'value'\n"
                "- Always qualify table names with schema (e.g. dbo.tablename)\n"
                "If this tool returns an error, explain the error and guide the user to fix it. Do NOT retry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "The SELECT query. Must include TOP or WHERE. Example: SELECT TOP 100 * FROM dbo.mytable"
                    },
                },
                "required": ["sql"],
            },
        ),
        Tool(
            name="list_tables",
            description=(
                "List all user tables in the MSSQL database with their schema names. "
                "Call this first to discover what tables are available before querying. "
                "Returns TABLE_SCHEMA and TABLE_NAME for each table. "
                "If this tool returns an error, explain the error and guide the user to fix it. Do NOT retry."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="describe_table",
            description=(
                "Show all column names and data types for a specific table. "
                "Call this to understand the structure of a table before writing a query. "
                "Use just the table name without schema prefix (e.g. 'mytable', not 'dbo.mytable'). "
                "If this tool returns an error, explain the error and guide the user to fix it. Do NOT retry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "table": {
                        "type": "string",
                        "description": "Table name only, without schema prefix. Example: 'orders' not 'dbo.orders'"
                    },
                },
                "required": ["table"],
            },
        ),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    ok, err = check_api_key(arguments)
    if not ok:
        return [TextContent(type="text", text=err)]

    if name == "query":
        sql = arguments.get("sql", "")
        ok, reason = validate_query(sql)
        if not ok:
            return [TextContent(type="text", text=f"Error: {reason}")]
        try:
            rows = await run_query(sql)
            print(f"[query] rows={len(rows)} sql={sql[:80]}", file=sys.stderr)
            return [TextContent(type="text", text=json.dumps(rows, ensure_ascii=False, indent=2, default=str))]
        except Exception as e:
            print(f"[query error] {e}", file=sys.stderr)
            return [TextContent(type="text", text=friendly_db_error(e))]

    elif name == "list_tables":
        try:
            rows = await run_query(
                "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE='BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME"
            )
            return [TextContent(type="text", text=json.dumps(rows, ensure_ascii=False, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=friendly_db_error(e))]

    elif name == "describe_table":
        table = arguments.get("table", "")
        if not table.replace("_", "").replace(".", "").isalnum():
            return [TextContent(type="text", text="Error: Invalid table name")]
        try:
            rows = await run_query(
                f"SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, CHARACTER_MAXIMUM_LENGTH "
                f"FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{table}' "
                f"ORDER BY ORDINAL_POSITION"
            )
            return [TextContent(type="text", text=json.dumps(rows, ensure_ascii=False, indent=2))]
        except Exception as e:
            return [TextContent(type="text", text=friendly_db_error(e))]

    return [TextContent(type="text", text=f"Error: Unknown tool {name}")]


async def main():
    if USE_HTTP:
        from mcp.server.streamable_http import StreamableHTTPServerTransport
        transport = StreamableHTTPServerTransport(port=HTTP_PORT, auth_check=verify_jwt)
        print(f"[mssql-mcp] HTTP mode, listening on port {HTTP_PORT}", file=sys.stderr)
        await app.run(transport.receive_stream, transport.send_stream, app.create_initialization_options())
    else:
        async with mcp.server.stdio.stdio_server() as (receive, send):
            print("[mssql-mcp] Stdio mode, waiting for connections...", file=sys.stderr)
            await app.run(receive, send, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
