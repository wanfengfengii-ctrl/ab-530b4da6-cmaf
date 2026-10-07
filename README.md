# playlist-audit

媒体质检服务：在发布采用 CMAF 字节范围复用的 HLS 点播清单前，证明每个媒体片段落在已登记资源内，并能形成无歧义的播放时间线。

纯 Python 标准库实现，零第三方依赖。

## API

### `POST /api/playlists/audit`

请求（JSON，清单为 ≤ 1 MiB 的 UTF-8 文本，`resources` 至多 128 个唯一 URI）：

```json
{
  "playlist": "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MAP:URI=\"video.mp4\",BYTERANGE=\"824@0\"\n#EXTINF:2.000000,\n#EXT-X-BYTERANGE:150000@824\nvideo.mp4\n#EXT-X-ENDLIST\n",
  "resources": {"video.mp4": 1000000}
}
```

约束：仅接纳带 `#EXT-X-ENDLIST` 的点播清单，最多 2000 个媒体片段；每段须有正数微秒精度
`EXTINF`、有效 `EXT-X-BYTERANGE` 和当前 `EXT-X-MAP`；`EXT-X-BYTERANGE` 省略偏移只可承接同
URI 的前一媒体范围；跨 `EXT-X-DISCONTINUITY` 后必须重新声明 `EXT-X-MAP`。

成功 `200`（按清单顺序给出媒体序号、epoch、资源 URI、初始化与媒体的半开字节区间及累计起止微秒；
序号与 epoch 分别遵循 `EXT-X-MEDIA-SEQUENCE` / `EXT-X-DISCONTINUITY-SEQUENCE` 声明，默认 0）：

```json
{
  "segment_count": 1,
  "total_duration_us": 2000000,
  "segments": [
    {
      "media_sequence": 0,
      "epoch": 0,
      "uri": "video.mp4",
      "init":  {"uri": "video.mp4", "start": 0,   "end": 824},
      "media": {"start": 824, "end": 150824},
      "start_us": 0,
      "end_us": 2000000
    }
  ]
}
```

失败 `422`，报告按清单行序最先出现的问题（`line` 为 1 起始行号，仅文件末尾才能发现的问题为 `null`）：

```json
{"error": {"code": "out_of_bounds", "message": "media byte range [900000, 1050000) exceeds length 1000000 of \"video.mp4\"", "line": 6}}
```

| code | 含义 |
| --- | --- |
| `unknown_resource` | 媒体或初始化段 URI 未登记 |
| `out_of_bounds` | 字节区间超出已登记资源长度 |
| `overlap` | 字节区间与同一资源的既有区间重叠（重复声明相同初始化区间除外） |
| `invalid_implicit_offset` | 省略偏移未承接同 URI 的前一媒体范围 |
| `missing_init` | 无当前 `EXT-X-MAP`，或 discontinuity 后未重新声明 |
| `invalid_extinf` / `invalid_byterange` / `invalid_map` | 标签取值非法（含非正数或非微秒精度 EXTINF） |
| `missing_extinf` / `missing_byterange` | 片段缺少必需标签 |
| `not_vod` | 缺少 `#EXT-X-ENDLIST` |
| `not_media_playlist` | 出现主清单标签 |
| `invalid_playlist` | 其他结构性问题（缺 `#EXTM3U`、ENDLIST 后仍有内容等） |
| `playlist_too_large` / `too_many_segments` / `too_many_resources` / `invalid_resource_length` | 超出请求约束 |

请求体本身非法（非 JSON、字段类型错误）返回 `400`；超过请求体上限返回 `413`。

### `GET /healthz`

健康检查，服务存活时返回 `200 {"status": "ok"}`。

## 运行

```bash
docker compose up --build              # API 监听 http://localhost:8080
API_PORT=9000 docker compose up --build # 可变宿主机端口
```

## 验证（一次性服务 verify）

`verify` 等待 API 健康检查就绪后，依次运行代码测试（unittest）、构建检查（字节码编译）
以及合法 / 越界清单的 API 冒烟，并以退出码汇总结果（全部通过为 0，否则为 1）：

```bash
docker compose run --rm verify
# 或
docker compose up --build --exit-code-from verify verify
```

## 本地开发（无 Docker）

```bash
python3 -m unittest discover tests        # 代码测试
python3 -m app.main                       # 启动 API（PORT 环境变量可改端口，默认 8000）
API_BASE_URL=http://127.0.0.1:8000 python3 -m app.verify   # 对运行中的 API 做完整验证
```
