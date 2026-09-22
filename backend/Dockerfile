FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    MODEL_ROOT=/models \
    FRONTEND_DIR=/frontend

WORKDIR /app

# Dependency layer: keyed on pyproject.toml alone. Installing the project from a
# metadata-only stub first means a code-only release no longer invalidates the whole
# site-packages layer, which used to force a full pip install plus a cross-border
# re-upload of every dependency on each tag push (the reason releases hung for tens
# of minutes when the registry path was slow).
COPY backend/pyproject.toml /app/pyproject.toml
COPY backend/app/__init__.py /app/app/__init__.py
RUN pip install --no-cache-dir .

# Application layer: the real code replaces the stub above (--no-deps keeps this
# local, so nothing is re-resolved or downloaded here).
COPY backend/app /app/app
RUN pip install --no-cache-dir --no-deps --force-reinstall .

COPY frontend /frontend

RUN mkdir -p /data /models

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).read()"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
