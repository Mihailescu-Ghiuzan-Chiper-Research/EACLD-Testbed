FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EACLD_NODES_CONFIG=/etc/eacld/nodes.json \
    EACLD_NODE_KEYS=/run/eacld/node_keys.json \
    EACLD_MQTT_PASSWORD_FILE=/run/eacld/mqtt_password

WORKDIR /app
COPY nodes/services/nodes/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY shared/eacld_contracts/ /app/eacld_contracts/
COPY nodes/services/nodes/src/ /app/src/

EXPOSE 8202 8300
CMD ["python", "-m", "src.nodes"]
