FROM registry.redhat.io/lightspeed-services-ocp/ocp-rules-rhel9:2026.07.14-2

USER root

ENV HOME=/app \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements-build.txt .
# hadolint ignore=DL3041  # We prefer to use latest versions of dnf pkgs
RUN microdnf install --nodocs -y sqlite postgresql-devel && \
    /opt/venv/bin/pip install --no-cache-dir -U -r requirements-build.txt && \
    microdnf clean all && \
    mkdir -p /tmp/insights-uploads && chmod 777 /tmp/insights-uploads

COPY requirements.txt .
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY config.yml .

RUN ln -sf /ccx-rules-ocp/content /app/content

EXPOSE 8000 8443

USER 1001

ENTRYPOINT []
CMD ["python", "-m", "app.main"]
