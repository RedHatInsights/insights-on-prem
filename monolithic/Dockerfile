# The image won't be used in the final implementation
FROM quay.io/redhat-services-prod/obsint-processing-tenant/rules-containers/rules-containers-private:latest AS rules

FROM registry.access.redhat.com/ubi9-minimal:latest

ENV HOME=/app \
    VENV=/app-venv \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    PYTHONUNBUFFERED=1 \
    PATH="/app-venv/bin:$PATH"

WORKDIR $HOME

# Copy CA certificates and update trust
COPY --from=rules /etc/pki/ca-trust/source/anchors/Current-IT-Root-CAs.pem /etc/pki/ca-trust/source/anchors/

# Install system dependencies and create venv in one layer
RUN microdnf install --nodocs -y sqlite python3.12 python3.12-devel gcc postgresql-devel file tar unzip && \
    python3.12 -m venv $VENV && \
    update-ca-trust && \
    pip install --no-cache-dir -U pip setuptools wheel && \
    microdnf clean all && \
    mkdir -p /tmp/insights-uploads && chmod 777 /tmp/insights-uploads

# Copy only requirements first (for better layer caching)
COPY requirements.txt .

# Install Python dependencies (cached unless requirements.txt changes)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Copy application code (changes here won't invalidate pip install layer)
COPY app ./app
COPY migrations ./migrations
COPY config.yml ./

# Copy rules content from rules image
COPY --from=rules /app/content/content /app/content

EXPOSE 8000

USER 1001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
