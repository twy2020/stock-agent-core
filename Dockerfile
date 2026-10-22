FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 120 -r requirements.txt
COPY agent_core ./agent_core
RUN addgroup --system app && adduser --system --ingroup app app && mkdir -p /data && chown app:app /data
USER app
EXPOSE 18210
CMD ["python", "-m", "agent_core", "--config", "/run/stock/core.json", "--host", "0.0.0.0", "--port", "18210"]
