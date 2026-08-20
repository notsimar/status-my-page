# Use official minimal Python runtime
FROM python:3.14-slim

# Install system dependencies (curl and ping for healthcheck capabilities)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and app directory
RUN useradd -m -u 1000 statuspage
WORKDIR /app

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt python-dotenv

# Copy application source code
COPY --chown=statuspage:statuspage . .

# Ensure instance, logs, and archives directories exist with right permissions
RUN mkdir -p instance logs archives && \
    chown -R statuspage:statuspage /app

# Switch to non-root user
USER statuspage

# Expose default application port
EXPOSE 8920

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    STATUS_PORT=8920

# Run with gunicorn
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8920", "--workers", "2", "--threads", "4"]
