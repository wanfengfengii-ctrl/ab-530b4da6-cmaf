# 媒体质检平台 —— HLS/CMAF 字节范围点播清单审核服务
# 纯 Python 标准库实现，无第三方运行时依赖。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QC_HOST=0.0.0.0 \
    QC_PORT=8080

WORKDIR /app

# 先复制源码（.dockerignore 负责排除缓存/虚拟环境）。
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests

# 容器内端口固定为 QC_PORT；宿主机端口由 docker-compose 的 HOST_PORT 决定。
EXPOSE 8080

# 健康检查：轮询 /healthz，不依赖 curl（slim 镜像默认未安装）。
HEALTHCHECK --interval=5s --timeout=3s --start-period=2s --retries=12 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('QC_PORT','8080'), timeout=2).status == 200 else 1)"

CMD ["python", "-m", "app.server"]
