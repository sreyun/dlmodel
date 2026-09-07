FROM python:3.11-slim
WORKDIR /app
COPY backend/pyproject.toml /app/
COPY backend/app /app/app
COPY frontend /frontend
RUN pip install --no-cache-dir .
ENV DATA_DIR=/data MODEL_ROOT=/models FRONTEND_DIR=/frontend
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
