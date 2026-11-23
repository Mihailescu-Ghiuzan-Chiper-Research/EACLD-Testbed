FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY reference/services/reference/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY shared/eacld_contracts/ /app/eacld_contracts/
COPY reference/services/reference/src/ /app/src/

EXPOSE 8201 8300
CMD ["python", "-m", "src.reference"]
