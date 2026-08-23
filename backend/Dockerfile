# backend/Dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install deps first so Docker can cache this layer — it only re-runs
# when requirements.txt actually changes, not on every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the actual application code
COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]