# Pasloe Clients

This directory contains official and community-contributed clients for the Pasloe event store. 

A "Client" in the Pasloe ecosystem is typically a background logger, an agent, a sensor, or any application that pushes structured events (and optionally large artifacts like images) into the Pasloe backend.

## How to Build a Custom Client

Building a new client for Pasloe is straightforward since it relies on standard HTTP REST APIs. You can implement a client in any language (Python, Rust, Go, Node.js, etc.).

Here is a guide on how to integrate your custom application with the Pasloe backend.

### 1. Authentication

If the Pasloe backend is configured with an `API_KEY`, your client must include it in the headers of **every** request:

```http
X-API-Key: your-secret-api-key
```

### 2. Pushing Standard Events

To log an activity or state change, send a `POST` request to `/events`.

**Endpoint:** `POST /events`

**Payload:**
```json
{
  "source_id": "my-custom-client",  // A unique identifier for your app/sensor
  "type": "my_app.action_occurred", // Event type string
  "data": {                         // Free-form JSON data payload
    "foo": "bar",
    "value": 42
  },
  "session_id": null                // Optional UUID for grouping events
}
```

*Note: If `source_id` does not exist in the database, Pasloe will automatically register it upon the first event.*

### 3. Handling Large Artifacts (Images/Files)

If your client needs to store large binary files (like screenshots or documents), you should **not** send the raw bytes to the `/events` endpoint. Instead, Pasloe provides an S3 integration using **Presigned URLs**.

**The Upload Flow:**

**Step A: Request a Presigned URL**
Ask Pasloe for a temporary upload link. Define a conceptual filename (directories/prefixes are supported) and the MIME type.

`POST /artifacts/presign`
```json
{
  "filename": "my-client/images/screenshot-1.png",
  "content_type": "image/png"
}
```

*Response:*
```json
{
  "upload_url": "https://s3.amazonaws.com/bucket/my-client/images/screenshot-1.png?AWSAccessKeyId=...&Signature=...",
  "access_url": "https://s3.amazonaws.com/bucket/my-client/images/screenshot-1.png",
  "object_name": "my-client/images/screenshot-1.png"
}
```

**Step B: Upload the File**
Perform a standard HTTP `PUT` request directly to the `upload_url` provided in the response. Set the `Content-Type` header to match what you requested.

```http
PUT <upload_url>
Content-Type: image/png

<BINARY DATA>
```

**Step C: Record the Event**
Now that the file is safely in S3, you can push a standard event to Pasloe, including the `access_url` so that other consumers can view the file.

`POST /events`
```json
{
  "source_id": "my-custom-client",
  "type": "file.uploaded",
  "data": {
    "url": "https://s3.amazonaws.com/bucket/my-client/images/screenshot-1.png",
    "size": 1024
  }
}
```

### Reference

For a complete, robust example of a client that captures screenshots, deduplicates them using dHash, handles S3 presigned URL uploads, and gracefully reconnects on failures, refer to the source code of the [pasloe-screenshot](./pasloe-screenshot/) Rust client.
