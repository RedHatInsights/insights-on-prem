FROM registry.redhat.io/lightspeed-services-ocp/ocp-rules-rhel9:2026.06.30

USER root

ENV HOME=/app \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0

WORKDIR /app

COPY requirements-build.txt .
# hadolint ignore=DL3041  # We prefer to use latest versions of dnf pkgs
RUN microdnf install --nodocs -y sqlite postgresql-devel git && \
    curl -so /tmp/jemalloc.rpm https://yum.oracle.com/repo/OracleLinux/OL9/developer/EPEL/x86_64/getPackage/jemalloc-5.2.1-2.el9.x86_64.rpm && \
    rpm -i /tmp/jemalloc.rpm && rm -f /tmp/jemalloc.rpm && \
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

# Memory allocator tuning — bypass pymalloc arenas so glibc malloc
# handles all allocations directly, and tune glibc to return freed
# pages to the OS more aggressively.
# jemalloc is installed but NOT enabled via LD_PRELOAD here because
# it crashes under QEMU emulation (ARM Mac building x86_64 images).
# Enable it in production via environment variable:
#   LD_PRELOAD=/usr/lib64/libjemalloc.so.2
#   MALLOC_CONF=background_thread:true,dirty_decay_ms:1000,muzzy_decay_ms:1000
ENV PYTHONMALLOC=malloc \
    MALLOC_TRIM_THRESHOLD_=65536 \
    MALLOC_MMAP_THRESHOLD_=65536

ENTRYPOINT []
CMD ["python", "-m", "app.main"]
