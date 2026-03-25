# Pasloe

Append-only event store for the [Yoitsu](https://github.com/guan-spicy-wolf/yoitsu) stack.

Pasloe now runs a two-stage architecture:

1. `ingest log` (durable accepted writes)
2. `read models` (committed events + projection/webhook pipelines)

Producers only need `accepted` from `POST /events`; consumers only read committed events.

## Key Features

- **Two-Stage Durability**: API writes to ingress first (`accepted`), then background commit pipeline makes events visible.
- **Crash-Recoverable Workers**: `committer`, `projector`, and `webhook` pipelines all use DB lease + retry.
- **Idempotent Ingest**: optional `idempotency_key` per source.
- **Cursor-Based Pagination**: Efficient committed-event querying using `X-Next-Cursor`.
- **Webhook Integration**: asynchronous HMAC-SHA256 delivery with retry.
- **PostgreSQL-First Deployment**: production defaults now target PostgreSQL.

---

## 🚀 Quick Start

### 1. Requirements

- [uv](https://github.com/astral-sh/uv) (Python package manager)
- Python 3.10+

### 2. Configuration

Set up your environment variables:

```bash
cp .env.example .env
# Edit .env to set your API_KEY, DATABASE_URL, and S3 credentials
```

### 3. Run the Server

```bash
uv sync
uv run uvicorn src.pasloe.app:app --host 0.0.0.0 --port 8000 --reload
```

---

## Web Management UI

Pasloe includes a built-in dashboard accessible at:

**`http://localhost:8000/ui`**

Features:
- **Webhooks**: Register callbacks, manage event filters, and view subscription status.
- **Sources**: Monitor registered data sources and their metadata.
- **Events**: Real-time inspection of the event stream with JSON payload formatting.

---

## API Overview

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/ui` | `GET` | Access the Web Management Dashboard |
| `/events` | `GET` | Query events with cursor pagination and filtering |
| `/events` | `POST` | Accept an event into ingress log (`202 accepted`) |
| `/sources` | `GET` | List all registered data sources |
| `/webhooks` | `GET` | List active webhook subscriptions |
| `/webhooks` | `POST` | Register or update a webhook callback |
| `/webhooks/{id}` | `DELETE` | Remove a webhook subscription |

**Authentication**: Include `X-API-Key: <your_key>` in the headers for all requests.

---

## Development

Run tests with `pytest`:

```bash
uv run pytest
```
