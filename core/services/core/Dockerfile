FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/vendor/EACLD_E0_Experimental_Package

WORKDIR /app
COPY core/services/core/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY shared/eacld_contracts/ /app/eacld_contracts/
COPY core/services/core/src/ /app/src/
COPY core/vendor/e0/e0_source.zip.b64 /tmp/e0_source.zip.b64

# This curated archive contains the unmodified E0 core, its fitted model,
# configuration and core tests, but no private ground-truth or episode data.
RUN python -c "import base64,io,pathlib,zipfile; raw=base64.b64decode(pathlib.Path('/tmp/e0_source.zip.b64').read_text()); z=zipfile.ZipFile(io.BytesIO(raw)); assert z.testzip() is None; z.extractall('/app/vendor')" \
 && python -c "import hashlib,pathlib; p=pathlib.Path('/app/vendor/EACLD_E0_Experimental_Package/eacld/core.py'); assert hashlib.sha256(p.read_bytes()).hexdigest()=='cedddf154553162a4bda1e12fc48045162af0918ffd975a001141ae2e4f05f58'"
RUN rm /tmp/e0_source.zip.b64

EXPOSE 8300
CMD ["python", "-m", "src.core"]
