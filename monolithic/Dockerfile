# Stage 1: Extract ccx packages and rules content from the source container
FROM quay.io/redhat-services-prod/obsint-processing-tenant/rules-containers/rules-containers-private:latest AS rules

# Extract installed packages list
RUN pip freeze > /tmp/base_requirements.txt

# Copy ccx packages to a known location for extraction
RUN mkdir -p /extracted/ccx && \
    cp -r /usr/local/lib/python3.11/site-packages/ccx_* /extracted/ccx/

# Stage 2: Build application image
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
RUN microdnf install --nodocs -y sqlite python3.11 python3.11-devel gcc postgresql-devel file tar unzip && \
    python3.11 -m venv $VENV && \
    update-ca-trust && \
    pip install --no-cache-dir -U pip setuptools wheel && \
    microdnf clean all && \
    mkdir -p /tmp/insights-uploads && chmod 777 /tmp/insights-uploads

# Copy ccx packages from rules image to site-packages
COPY --from=rules /extracted/ccx /tmp/ccx
RUN SITE_PACKAGES=$(python3 -c "import site; print(site.getsitepackages()[0])") && \
    cp -r /tmp/ccx/* ${SITE_PACKAGES}/ && \
    rm -rf /tmp/ccx

# Copy base requirements from rules image and project requirements
COPY --from=rules /tmp/base_requirements.txt /tmp/base_requirements.txt
COPY requirements.txt .

# Install Python dependencies (cached unless requirements change)
# ccx packages are already installed (copied with .dist-info), pip will skip them
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r /tmp/base_requirements.txt && \
    pip install -r requirements.txt && \
    rm /tmp/base_requirements.txt

# Copy application code (changes here won't invalidate pip install layer)
COPY app ./app
COPY migrations ./migrations
COPY config.yml ./

# Copy rules content from rules image
COPY --from=rules /app/content/content /app/content

EXPOSE 8000 8443

USER 1001

CMD ["python", "-m", "app.main"]
