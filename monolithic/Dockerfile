FROM registry.redhat.io/lightspeed-services-ocp/ocp-rules-rhel9:2026.06.30

USER root

ENV HOME=/app \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    PYTHONUNBUFFERED=1

WORKDIR /app

# hadolint ignore=DL3041  # We prefer to use latest versions of dnf pkgs
# gcc, gcc-c++, python3.12-devel, rust and cargo are build-only tools needed
# to compile Python packages from source (pydantic-core, uvloop, httptools,
# watchfiles, greenlet, ...). gcc-c++ is required for C++ extensions (greenlet).
# python3.12-devel (not python3-devel) is required because this base image
# ships python3.12 directly, not the RHEL9 default python3 (3.9).
# requirements-build.txt is not installed here: it only tells Hermeto/Cachi2
# which build-backend sdists to prefetch, so `pip install -r requirements.txt`
# below can resolve them from the offline index during a hermetic build.
# hadolint ignore=DL3041
RUN microdnf install --nodocs -y sqlite postgresql-devel gcc gcc-c++ python3.12-devel rust cargo && \
    /opt/venv/bin/pip install --no-cache-dir -U pip setuptools wheel && \
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
