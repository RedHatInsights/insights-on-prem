FROM quay.io/redhat-services-prod/obsint-processing-tenant/rules-containers/rules-containers-private:latest AS rules

FROM registry.access.redhat.com/ubi9-minimal:latest

ENV HOME=/app \
    VENV=/app-venv \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt

# Download Red Hat corporate certificates - same as ccx-data-pipeline
ADD https://certs.corp.redhat.com/certs/2022-IT-Root-CA.pem https://certs.corp.redhat.com/certs/Current-IT-Root-CAs.pem /etc/pki/ca-trust/source/anchors/

WORKDIR $HOME

COPY --from=rules /app/content/content /app/content

ENV PATH="$VENV/bin:$PATH"

# Install dependencies and setup - matching ccx-data-pipeline approach
# Added: file, tar, unzip - required by insights-core for archive processing
RUN microdnf install --nodocs -y sqlite python3.11 python3.11-devel gcc postgresql-devel file tar unzip && \
    python3.11 -m venv $VENV && \
    update-ca-trust && \
    pip install --no-cache-dir -U pip setuptools wheel && \
    microdnf clean all

# Create temp upload directory
RUN mkdir -p /tmp/insights-uploads && chmod 777 /tmp/insights-uploads

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy application code
COPY . $HOME

RUN pip install --no-cache-dir -r requirements.txt

# Expose port
EXPOSE 8000

USER 1001

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
