# API image: FastAPI + LangGraph, running as an unprivileged user.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /app/logs \
    && chown app /app/logs

USER app
EXPOSE 8000
CMD ["uvicorn", "delivery_agent.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
