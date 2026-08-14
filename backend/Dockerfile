FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY agentlens agentlens
RUN pip install --no-cache-dir .
COPY alembic alembic
COPY alembic.ini .
CMD ["uvicorn", "agentlens.api:app", "--host", "0.0.0.0", "--port", "8000"]
