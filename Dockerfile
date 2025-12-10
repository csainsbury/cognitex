FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy application code and install
COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Create non-root user
RUN useradd --create-home --shell /bin/bash cognitex
RUN chown -R cognitex:cognitex /app
USER cognitex

# Default command runs the API server
CMD ["python", "-m", "uvicorn", "cognitex.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
