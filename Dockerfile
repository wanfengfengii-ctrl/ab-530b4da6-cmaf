# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY app ./app
COPY tests ./tests

RUN useradd --system --uid 10001 appuser \
    && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000
CMD ["python", "-m", "app.main"]
