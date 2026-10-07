# 媒体质检平台 —— CMAF 字节范围 HLS 点播清单审核

在发布采用 **CMAF 字节范围复用（EXT-X-BYTERANGE）** 的 HLS 点播清单前，
证明每个片段都落在**已登记资源长度**之内，且能形成**无歧义的播放时间线**。

- 纯 Python 3.11 标准库实现，**零第三方运行时依赖**，离线可构建。
- 接口：`POST /api/playlists/audit`、健康检查 `GET /healthz`。

## 接口约定

### `POST /api/playlists/audit`

请求（`application/json`，UTF-8）：

```json
{
  "manifest": "#EXTM3U\n...\n#EXT-X-ENDLIST\n",
  "resource_lengths": {
    "https://cdn/v.mp4": 123456
  }
}
```

约束：

| 项目 | 限额 |
| --- | --- |
| `manifest` 大小 | ≤ 1 MiB（UTF-8 字节） |
| 唯一资源 URI | ≤ 128 个（`resource_lengths` 的键） |
| 媒体片段数 | ≤ 2000 |

清单必须满足：

1. 媒体播放列表且以 `#EXTM3U` 开头，携带 `#EXT-X-ENDLIST`（仅 VOD）；
2. 每个媒体片段有**正数**、微秒精度的 `#EXTINF`（>0，换算到整数微秒）；
3. 每个媒体片段有 `#EXT-X-BYTERANGE: n[@o]`；
   省略偏移 `o` 时只能承接**同一 URI** 的前一媒体范围末尾；
4. 存在当前生效的 `#EXT-X-MAP`，且 MAP 的 `BYTERANGE` 必须含显式偏移；
5. `#EXT-X-DISCONTINUITY` 之后、下一个媒体片段之前必须重新声明 `#EXT-X-MAP`；
6. 所有 MAP 与媒体区间都落在 `resource_lengths` 登记的资源内，
   且同一资源内互不重叠（半开区间，相邻 `[a,b) [b,c)` 合法）。

成功响应（`200`，按清单顺序）：

```json
{
  "status": "ok",
  "segment_count": 1,
  "segments": [
    {
      "sequence": 0,
      "epoch": 0,
      "uri": "v.mp4",
      "init_range":  { "start": 0,  "end": 90 },
      "media_range": { "start": 90, "end": 100 },
      "start_us": 0,
      "end_us": 2000000
    }
  ]
}
```

- `sequence`：遵循 `#EXT-X-MEDIA-SEQUENCE`，缺省为 **0**，逐段 +1；
- `epoch`：遵循 `#EXT-X-DISCONTINUITY-SEQUENCE`，缺省为 **0**，
  每遇 `#EXT-X-DISCONTINUITY` +1；
- `start_us`/`end_us`：从 0 起按 EXTINF 微秒累计的无间断时间线。

失败响应（`4xx`）按**清单物理行序**报告最先出现的问题：

```json
{ "error": { "code": "out_of_bounds", "message": "...", "line": 9 } }
```

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 422 | `playlist_error` | 清单语法/语义不合法（缺 ENDLIST、缺 MAP、EXTINF 非正等） |
| 422 | `unknown_resource` | MAP 或片段 URI 未在 `resource_lengths` 登记 |
| 422 | `out_of_bounds` | 区间超出登记长度 |
| 422 | `overlapping_range` | 同一资源内区间重叠 |
| 400 | `bad_request` / `invalid_json` / `invalid_utf8` | 请求格式问题 |
| 413 | `payload_too_large` | manifest 超过 1 MiB |
| 415 | `unsupported_media_type` | Content-Type 非 JSON |

## 本地运行（无需 Docker）

```bash
python3 -m app.server                 # 默认 0.0.0.0:8080
QC_PORT=9090 python3 -m app.server    # 自定义端口
python3 -m unittest discover -s tests # 60 个单元/HTTP 测试
```

## Docker 与一键校验

```bash
# 构建并启动 API + 一次性 verify 服务（verify 结束后以退出码汇总）
docker compose build
docker compose up api verify

# 自定义宿主机端口
HOST_PORT=9090 docker compose up api verify
```

- `api`：业务服务，容器内 8080，宿主机端口由 `HOST_PORT` 决定（默认 8080），
  内置 `HEALTHCHECK` 轮询 `/healthz`。
- `verify`：**一次性服务**，`depends_on: service_healthy` 保证 API 就绪后才运行；
  依次执行①等待就绪 ②代码测试（unittest）③构建检查（compileall）
  ④合法 / 越界 / 未知资源 / 重叠 / 无 ENDLIST 清单的 API 冒烟，
  全部通过退出 `0`，否则退出 `1`。

仅查看 verify 退出码：

```bash
docker compose up verify; docker inspect --format '{{.State.ExitCode}}' \
  $(docker compose ps -aq verify)
```
