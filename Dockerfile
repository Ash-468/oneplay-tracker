# Ultra-lightweight Dockerfile for OnePlay Tracker
# Image size: ~45MB | RAM usage: ~15MB (Zero external pip dependencies)
FROM python:3.12-alpine

# Set Python to run unbuffered for real-time streaming logs
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Install CA certificates for secure HTTPS API calls
RUN apk add --no-cache ca-certificates tzdata

WORKDIR /app

# Copy application script
COPY tracker.py /app/tracker.py

EXPOSE 8080

ENTRYPOINT ["python", "/app/tracker.py"]
