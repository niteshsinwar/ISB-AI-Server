# project_root/Dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED 1
ENV PYTHONDONTWRITEBYTECODE 1

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends poppler-utils tesseract-ocr && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# --- Add these lines to copy certificates ---
# This copies the certs from your local 'certs' folder (relative to Dockerfile)
# to /app/certs inside the container image.
RUN mkdir -p /app/certs

# Copy the certificate files from your local 'apps/certs' directory
# to '/app/certs/' inside the Docker image.
COPY certs/salesforcechain.key /app/certs/salesforcechain.key
COPY certs/salesforcechain.crt /app/certs/salesforcechain.crt

COPY ./app ./app

COPY main.p? .

EXPOSE 443
CMD ["python", "main.py"]
