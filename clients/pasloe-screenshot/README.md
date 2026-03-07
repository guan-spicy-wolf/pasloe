# pasloe-screenshot

pasloe 的截图事件客户端，定期捕获屏幕截图并将事件推送到 [pasloe](../../) 引擎。

## 功能

- 多显示器并发截图
- 基于 dHash 的图像去重（内容未变化时跳过）
- 截图存储到本地或 S3
- 将截图事件推送到 pasloe（`source_id: pasloe-screenshot`, `type: screenshot.captured`）

## 快速开始

### 1. 配置

复制示例配置并按需修改：

```bash
cp config/config.example.toml config/config.toml
```

最小配置（本地存储）：

```toml
[pasloe]
url = "http://localhost:8000"
api_key = "your-api-key"

[storage.local]
enable = true
path = "/tmp/screenshots"

[monitors.MY_MONITOR_1920_1080_0_0]
enable = true
interval = 1000        # 检查间隔（毫秒）
enforce_interval = 30000  # 最小截图间隔（毫秒）
dhash_resolution = 16
dhash_threshold = 10
```

### 2. 查找显示器 ID

```bash
cargo run -- list-monitors
```

输出示例：

```
检测到 2 个显示器:

  [1] GS27QK_2560_1440_0_0
  [2] GS27QK_2560_1440_-2560_0
```

将显示器 ID 填入配置文件的 `[monitors.<ID>]` 节。

### 3. 运行

```bash
cargo run -- capture --config config/config.toml
```

## 配置说明

### `[pasloe]`

| 字段 | 说明 |
|------|------|
| `url` | pasloe 服务地址 |
| `api_key` | pasloe API Key（对应 `X-API-Key` 请求头） |

### `[storage.local]`

| 字段 | 说明 |
|------|------|
| `enable` | 是否启用本地存储 |
| `path` | 截图保存目录 |

### `[storage.s3]`

| 字段 | 说明 |
|------|------|
| `enable` | 是否启用 S3 存储 |
| `prefix` | S3 对象前缀（目录） |

**注意**: 新版客户端的 S3 凭据由 Pasloe Server 管理，因此 `[storage.s3]` 不再需要配置 `bucket`、`endpoint` 和 `access_key`/`secret_key`。客户端会通过 API 动态获取 Presigned URL。

### `[monitors.<ID>]`

| 字段 | 说明 |
|------|------|
| `enable` | 是否启用该显示器 |
| `interval` | 检查间隔（毫秒），建议 1000 |
| `enforce_interval` | 两次截图的最小间隔（毫秒） |
| `dhash_resolution` | dHash 计算分辨率，越大越精细 |
| `dhash_threshold` | 相似度阈值（0-255），越小越敏感 |

### `[window]`

捕获当前焦点窗口（可选），字段同 `[monitors]`，额外有 `enable_ocr`（暂未实现）。

## 事件格式

推送到 pasloe 的事件结构：

```json
{
  "source_id": "pasloe-screenshot",
  "type": "screenshot.captured",
  "data": {
    "url": "https://s3.example.com/bucket/screenshots/pasloe-screenshot/GS27QK_2560_1440_0_0/20260225_093000_000.png",
    "monitor_id": "GS27QK_2560_1440_0_0"
  },
  "session_id": null
}
```

可在 pasloe 中使用 Webhooks 订阅匹配 `screenshot.captured` 的事件来触发通知。

## 构建

```bash
cargo build --release
```
