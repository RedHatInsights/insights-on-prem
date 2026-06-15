# Stage 2: Build application image
FROM registry.access.redhat.com/ubi9-minimal:latest

ENV HOME=/app \
    VENV=/app-venv \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    PYTHONUNBUFFERED=1 \
    PATH="/app-venv/bin:$PATH"

WORKDIR $HOME


# Install system dependencies and create venv in one layer
RUN microdnf install --nodocs -y sqlite python3.11 python3.11-devel gcc postgresql-devel file tar unzip && \
    python3.11 -m venv $VENV && \
    update-ca-trust && \
    pip install --no-cache-dir -U pip setuptools wheel && \
    microdnf clean all && \
    mkdir -p /tmp/insights-uploads && chmod 777 /tmp/insights-uploads



# Install Python dependencies (cached unless requirements change)
# ccx packages are already installed (copied with .dist-info), pip will skip them
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install insights-core-messaging

# Copy application code (changes here won't invalidate pip install layer)
COPY app ./app
COPY migrations ./migrations
COPY config.yml ./

EXPOSE 8000

USER 1001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
