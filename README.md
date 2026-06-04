# mssql-mcp-server-python

A read-only MCP (Model Context Protocol) server for MS SQL Server, written in Python. Lets AI agents query your database safely — no writes, no stored procedures, no shell access.

## Features

- **Read-only** — only `SELECT` queries allowed
- **Two modes** — stdio (local) or HTTP + JWT (remote)
- **Official driver** — uses `pyodbc` with system ODBC driver
- **No third-party auth libraries** — JWT verified with Python built-in `hmac` + `hashlib`
- **Query safety** — blocks dangerous keywords, requires `TOP` or `WHERE`

## Tools exposed to AI agents

| Tool | Description |
|---|---|
| `list_tables` | List all tables in the database |
| `describe_table` | Show columns and types for a table |
| `query` | Run a read-only SELECT query |

## Requirements

- Python >= 3.11
- ODBC Driver for SQL Server installed on the machine

Check available ODBC drivers:

```bash
python -c "import pyodbc; print(pyodbc.drivers())"
```

If none installed, download from: https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server

## Installation

**Step 1 — Clone the repo:**

```cmd
git clone https://github.com/GarfieldHuang/mssql-mcp-server-python.git
cd mssql-mcp-server-python
```

**Step 2 — Install Python dependencies:**

```cmd
pip install -r requirements.txt
```

**Step 3 — Set up environment variables:**

```cmd
copy .env.example .env
```

Open `.env` and fill in your DB credentials and `API_KEY`.

**Step 4 — Generate API_KEY:**

```cmd
python -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output into `.env` as `API_KEY=...`.

## Usage

### Stdio mode — 本機 AI agent 使用

Stdio 模式不需要手動啟動 server。AI agent（如 Claude Code）會在需要時自動啟動 `server.py`，透過 stdin/stdout 溝通，用完自動關閉。

在 `claude_desktop_config.json` 登記一次即可：

```json
{
  "mcpServers": {
    "mssql": {
      "command": "python",
      "args": ["C:/path/to/server.py"]
    }
  }
}
```

登記後重啟 Claude Code，即可直接用自然語言查詢資料庫，無需手動執行任何指令。

### HTTP mode — 遠端 AI agent 使用

如果 AI agent 在其他機器上，需要改用 HTTP 模式。在能連到 MSSQL 的機器上執行：

```cmd
python server.py --http
```

Server 會持續在背景執行（保持 cmd 視窗開著），監聽 `HTTP_PORT`（預設 3000）。  
遠端 agent 透過 HTTPS + JWT token 連線：

```http
POST http://your-server:3000/mcp
Authorization: Bearer <jwt-token>
```

## Testing

Three test JSON files are included. Before running, edit each file and replace `your-api-key` with the value from your `.env`.

**Step 1 — Check server starts and lists tools:**

```cmd
python server.py < test-list-tools.json
```

Expected output: JSON with `query`, `list_tables`, `describe_table`.

**Step 2 — List tables in your database:**

```cmd
python server.py < test-list-tables.json
```

Expected output: JSON array of table names.

**Step 3 — Run a query:**

Edit `test-query.json` and replace `your_table` with a real table name, then:

```cmd
python server.py < test-query.json
```

Expected output: JSON array of rows.

> **Windows tip:** If `<` redirection doesn't work in cmd, use PowerShell:
> ```powershell
> Get-Content test-query.json | python server.py
> ```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DB_SERVER` | Yes | MSSQL hostname or IP |
| `DB_PORT` | No | Default: 1433 |
| `DB_DATABASE` | Yes | Database name |
| `DB_USER` | Yes | DB username (use a read-only account) |
| `DB_PASSWORD` | Yes | DB password |
| `DB_DRIVER` | No | ODBC driver name, default: `ODBC Driver 18 for SQL Server` |
| `API_KEY` | Yes | Secret key agents must pass in tool calls |
| `HTTP_PORT` | No | HTTP mode port, default: 3000 |
| `JWT_SECRET` | HTTP only | Secret for JWT verification |

## Create a read-only DB user (MSSQL)

```sql
CREATE LOGIN mcp_agent WITH PASSWORD = 'strong-password';
CREATE USER mcp_agent FOR LOGIN mcp_agent;
EXEC sp_addrolemember 'db_datareader', 'mcp_agent';
```

