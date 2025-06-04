# project_root/Dockerfile

# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
# PYTHONUNBUFFERED ensures that Python output is sent straight to the terminal without being buffered first.
ENV PYTHONUNBUFFERED 1
# PYTHONDONTWRITEBYTECODE prevents Python from writing .pyc files to disc.
ENV PYTHONDONTWRITEBYTECODE 1

# Set the working directory in the container
WORKDIR /app

# Install Poppler for pdf2image (Debian/Ubuntu based)
# Ensure this matches the OS of the python:3.11-slim image (likely Debian)
# If using a different base image (e.g., Alpine), the Poppler installation command will differ.
RUN apt-get update && \
    apt-get install -y --no-install-recommends poppler-utils tesseract-ocr && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# Using --no-cache-dir reduces the image size by not storing the pip download cache.
# Using --compile can slightly speed up startup time for compiled packages, but adds to build time.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY ./app ./app
COPY ./main.py .

# Make port 15841 available to the world outside this container (or your configured API_PORT)
# This should match the port Uvicorn runs on.
EXPOSE 15841

# Define the command to run the application using Uvicorn.
# The root main.py will read API_HOST and API_PORT from environment variables.
# Ensure your .env file or environment variables are set correctly in your deployment.
# For production, you might use Gunicorn as a process manager in front of Uvicorn workers.
# Example with Gunicorn (install gunicorn in requirements.txt):
# CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000", "app.main:app"]
# For simplicity, using Uvicorn directly as in the original main.py:
CMD ["python", "main.py"]
