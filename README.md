<div align="center">

# ALICE BRAIN

**Kho tri thức self-hosted có truy hồi multi-hop và citation truy vết được về tận nguồn.**

[![License: MIT](https://img.shields.io/badge/License-MIT-black.svg)](LICENSE)
[![Engine](https://img.shields.io/badge/engine-ALICE%20CORE-6E56CF)](https://github.com/blueberry-sensei/alice-core)
[![Stack](https://img.shields.io/badge/stack-Next.js%20%2B%20FastAPI-000000)](#kiến-trúc)
[![Runtime](https://img.shields.io/badge/runtime-Docker-2496ED?logo=docker&logoColor=white)](#chạy-nhanh)
[![Protocol](https://img.shields.io/badge/protocol-MCP-2EA043)](#mcp)

</div>

---

## Tổng quan

Đổ tài liệu vào, nhận lại tri thức **tìm được, nối được, và kiểm chứng được**.

Khác với kho tài liệu thường, ALICE BRAIN không lưu đoạn văn rồi so khớp vector. Nó bóc mỗi đoạn thành **event** (mệnh đề có nghĩa trọn vẹn) và **entity** (điểm neo), rồi lúc truy vấn thì nối các event qua entity chung. Nhờ vậy nó kéo về được cả tri thức liên quan **gián tiếp** — thứ mà tìm kiếm theo độ giống bỏ sót.

Mọi thứ chạy trên máy bạn. LLM là thành phần duy nhất cần credential.

| Thành phần | Chạy ở đâu | Cần key? |
|---|---|---|
| Engine [ALICE CORE](https://github.com/blueberry-sensei/alice-core) | cục bộ | không |
| Embedding `bge-m3` | cục bộ (bundled) | không |
| Bóc tách tài liệu (MarkItDown) | cục bộ | không |
| **LLM** — trích xuất & trả lời | provider bạn chọn | **có** |

## Chạy nhanh

Engine nằm ở repo riêng, nên build cần cả hai:

```bash
git clone https://github.com/blueberry-sensei/alice-brain.git
```

```bash
git clone https://github.com/blueberry-sensei/alice-core.git
```

```bash
cd alice-brain && docker compose up -d --build
```

Mở **http://localhost:3000**, tạo danh tính, rồi vào **Settings → Models** dán key LLM.

> Muốn gọn hơn nữa? [**ALICE CODING**](https://github.com/blueberry-sensei/alice-coding) có launcher một lệnh lo trọn gói: clone, `.env`, build, và kéo model embedding.

`apps/api/Dockerfile` nạp engine qua build context phụ tên `alicecore` — cần **Docker Compose ≥ 2.17** và BuildKit.

## Năng lực

| | |
|---|---|
| 🔍 **Truy hồi multi-hop** | Event nối nhau qua entity chung, bung ra tri thức liên quan gián tiếp. |
| 📎 **Citation truy vết được** | Mọi câu trả lời trỏ ngược về đoạn nguồn — kiểm chứng được, không phải tin suông. |
| 🕸️ **Explore mode** | Duyệt đồ thị event–entity trực quan thay vì gõ truy vấn mò. |
| 🔌 **MCP** | Mọi nguồn dữ liệu expose qua MCP; agent nào cũng cắm được. |
| 🤖 **Agent tích hợp** | Lõi điều phối riêng: tool, phê duyệt, huỷ giữa chừng, stream sự kiện. |
| 🔒 **Local-first** | Dữ liệu nằm trên máy bạn. Không gateway bên thứ ba nào cấu hình sẵn. |
| 🐘 **Sẵn sàng production** | Mặc định SQLite + LanceDB; đổi sang PostgreSQL/pgvector bằng một file override. |

## Kiến trúc

```
alice-brain/
├── apps/
│   ├── api/                    FastAPI
│   │   ├── sag_api/sag/            adapter engine — nơi DUY NHẤT import alicecore
│   │   ├── sag_api/connectors/     thu thập dữ liệu + registry
│   │   ├── sag_api/parsing/        MarkItDown, cục bộ
│   │   ├── sag_api/jobs/           hàng đợi nền (ingest → extract)
│   │   ├── sag_api/generation/     truy hồi → câu trả lời + citation
│   │   ├── sag_api/mcp/            MCP server (Streamable HTTP + stdio)
│   │   └── sag_agent/              lõi điều phối agent
│   ├── web/                    Next.js — workspace, explore mode, settings
│   └── desktop/                vỏ Electron
├── compose.yaml                stack dev
├── compose.postgres.yaml       override PostgreSQL/pgvector
└── skills/                     skill đóng gói cho agent
```

Chi tiết backend: [`apps/api/README.md`](apps/api/README.md).

## MCP

Backend expose nguồn dữ liệu theo chuẩn **Model Context Protocol** — Streamable HTTP tại `/mcp/`, hoặc stdio. Agent nào nói MCP đều dùng được cùng một bộ não: Claude Code, Codex, Gemini, opencode.

```bash
docker compose exec -i api python -m sag_api.mcp.server
```

## Cấu hình

Không có gateway bên thứ ba nào được cấu hình sẵn. Để trống Base URL là dùng endpoint chính chủ của provider.

| Biến | Ý nghĩa |
|---|---|
| `SAG_LLM_PROVIDER` | `openai` · `anthropic` · `gemini` |
| `SAG_LLM_API_KEY` | Key của provider (hoặc điền trong **Settings → Models**) |
| `SAG_EMBEDDING_MODEL` | Mặc định `bge-m3` |
| `SAG_SAG_LANGUAGE` | Ngôn ngữ prompt trích xuất: `en` · `vi` |

Danh sách đầy đủ: [`.env.example`](.env.example).

## Phát triển cục bộ

```bash
cd apps/api && pip install ../../alice-core && pip install -e ".[dev]" && uvicorn sag_api.main:app --reload --port 8000
```

```bash
cd apps/web && npm install && npm run dev
```

## Production

```bash
docker compose -f compose.yaml -f compose.postgres.yaml up -d --build
```

Đặt `SAG_SECRET_KEY` bằng giá trị ngẫu nhiên mạnh (`openssl rand -hex 32`), giới hạn `BIND_ADDRESS`, và đưa dịch vụ ra ngoài qua reverse proxy có kiểm soát truy cập.

## Giao diện

Tiếng Việt và tiếng Anh. Bản dịch tiếng Việt đang được hoàn thiện dần.

## License

[MIT](LICENSE).
