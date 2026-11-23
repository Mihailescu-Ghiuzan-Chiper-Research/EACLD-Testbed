FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EACLD_G1_MQTT_PASSWORD_FILE=/run/eacld/mqtt_password

WORKDIR /app
COPY edge/services/g1/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY shared/eacld_contracts/ /app/eacld_contracts/
COPY edge/services/g1/src/ /app/src/

EXPOSE 8300
CMD ["python", "-m", "src.g1"]
