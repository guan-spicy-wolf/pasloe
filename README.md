# Pasloe

Pasloe is a lightweight, append-only event store service for the Palimpsest V2 system. This `v2` branch replaces the complex legacy Pasloe with a much simpler event store, previously known as `eventstore`.

## Features
- Append-only event store (SQLite or PostgreSQL)
- Support for Cursor-based pagination on events (`X-Next-Cursor`)
- Registration and callback via Webhooks for newly inserted events
- Built-in support for issuing **S3 Presigned URLs** via the API for seamless client uploads without shipping S3 credentials.
- Retains compatibility with the original `pasloe-screenshot` background task rust agent.

## Deployment

### 1. Configuration

Copy the example environment into place and adjust S3 details or DB URL.

```bash
cp .env.example .env
```

### 2. Run

```bash
uv sync
uv run uvicorn pasloe.app:app --reload
```

## API usage

- **`POST /artifacts/presign`**
  Generate an S3 presigned URL for secure object uploads.
- **`GET /events`**
  Uses query param `cursor`. When another page exists, the response header `X-Next-Cursor` is sent.
- **`POST /events`**
  Save a newly created event into the pipeline.

## Clients
- `clients/pasloe-screenshot`: A Rust screenshot client which streams desktop screenshots and deduplicates using dHash, pushing events (with images) periodically relying on Pasloe's S3 presign endpoints.
