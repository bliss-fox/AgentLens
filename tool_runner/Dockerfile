FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

RUN useradd --create-home --uid 10002 runner
USER runner

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8200"]
