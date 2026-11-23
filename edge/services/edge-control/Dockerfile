FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY edge/services/edge-control/src/ /app/src/
CMD ["python", "-m", "src.agent"]
