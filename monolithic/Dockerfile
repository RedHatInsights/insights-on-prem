#FROM registry.redhat.io/lightspeed-services-ocp/ocp-rules-rhel9:2026.06.30
FROM localhost/lightspeed-services-ocp/ocp-rules-rhel10:2026.08.12

USER root

ENV HOME=/app \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0

WORKDIR /app

COPY requirements-build.txt .
# hadolint ignore=DL3041  # We prefer to use latest versions of dnf pkgs
# 'file' utility is required by insights-core for archive processing
RUN microdnf install --nodocs -y file sqlite postgresql-devel git && \
    /usr/local/bin/pip3 install --no-cache-dir -U -r requirements-build.txt && \
    microdnf clean all && \
    mkdir -p /tmp/insights-uploads && chmod 777 /tmp/insights-uploads

COPY requirements.txt .
RUN /usr/local/bin/pip3 install --no-cache-dir -r requirements.txt

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
ENV PYTHONMALLOC=mimalloc \
    MIMALLOC_PURGE_DELAY=0 \
    MIMALLOC_PURGE_DECOMMITS=1 \
    MIMALLOC_ARENA_EAGER_COMMIT=0 \
    MIMALLOC_LARGE_OS_PAGES=0 \
    MIMALLOC_SHOW_STATS=1 \
    MALLOC_TRIM_THRESHOLD=0 \
    MALLOC_ARENA_MAX=1

ENTRYPOINT []
CMD ["python", "-m", "app.main"]
