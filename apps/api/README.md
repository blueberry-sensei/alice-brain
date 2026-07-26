# alice-brain-api

Backend của ALICE: **FastAPI** + engine [`alicecore`](https://github.com/blueberry-sensei/alice-core).

## Phân tầng

| Tầng | Thư mục | Trách nhiệm |
|---|---|---|
| Adapter engine | `sag_api/sag/` | **Nơi duy nhất** import `alicecore`; nguồn dữ liệu ↔ `DataEngine` |
| Connector | `sag_api/connectors/` | Trừu tượng thu thập + registry (upload file → sync động) |
| Bóc tách tài liệu | `sag_api/parsing/` | Markdown đi thẳng; định dạng khác qua MarkItDown — **toàn bộ chạy cục bộ** |
| Hàng đợi job | `sag_api/jobs/` | Điều phối xử lý nền (máy trạng thái ingest → extract) |
| Sinh câu trả lời | `sag_api/generation/` | Kết quả truy hồi → câu trả lời LLM dạng stream + citation |
| Tool | `sag_api/tools/` | Tool cho agent: truy hồi/entity nội bộ + adapter MCP từ xa (chung interface `Tool`) |
| Agent Core | `sag_agent/` | Lõi điều phối độc lập: vòng đời, event, tool, phê duyệt, huỷ, cổng lưu trữ |
| Adapter agent | `sag_api/services/agent_service.py` | Nối model, tool, hội thoại vào Agent Core |
| MCP | `sag_api/mcp/` | Nguồn dữ liệu là MCP: FastMCP server + mount Streamable-HTTP (`/mcp/`) + lối vào stdio |
| Domain service | `sag_api/services/` | Logic nghiệp vụ thuần, không phụ thuộc FastAPI |
| Interface | `sag_api/api/v1/` | Route HTTP, chỉ làm IO / validate / serialize |

## Chạy

`alicecore` là package **cục bộ**, không có trên PyPI — cài nó trước:

```bash
python -m venv .venv && . .venv/bin/activate
pip install /đường/dẫn/alice-core
pip install -e ".[dev]"
cp .env.example .env
uvicorn sag_api.main:app --reload --host 0.0.0.0 --port 8000
```

Docs UI: http://localhost:8000/docs

Cách dựng bằng Docker (khuyên dùng — tự lo cả `alicecore` lẫn embedding cục bộ): xem `compose.yaml` ở gốc repo, hoặc launcher của [alice-coding](https://github.com/blueberry-sensei/alice-coding).

Dev server mặc định nghe mọi card mạng để truy cập được từ LAN; production hãy đưa ra ngoài qua reverse proxy có kiểm soát truy cập.

## Dịch vụ ngoài

Không có gateway bên thứ ba nào được cấu hình sẵn. Thứ duy nhất cần credential là **LLM** — tự chọn provider (OpenAI-compatible / Anthropic / Gemini) rồi điền trong Settings hoặc `.env`. Embedding và bóc tách tài liệu chạy cục bộ.
