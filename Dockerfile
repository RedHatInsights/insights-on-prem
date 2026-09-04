FROM registry.redhat.io/lightspeed-services-ocp/ocp-rules-rhel9:2026.08.25

USER root

ENV HOME=/app \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    PYTHONUNBUFFERED=1

WORKDIR /app

# hadolint ignore=DL3041
RUN /opt/venv/bin/pip install --no-cache-dir -U pip setuptools wheel && \
    mkdir -p /tmp/insights-uploads && chmod 777 /tmp/insights-uploads

COPY requirements.txt .
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY config.yml .

RUN ln -sf /ccx-rules-ocp/content /app/content

EXPOSE 8000 8443

USER 1001

# Override labels inherited from the OCP rules base image so release
# check-labels validates against the ACM product (lightspeed-services-acm).
LABEL name="lightspeed-services-acm/ocp-rules-rhel9" \
      cpe="cpe:/a:redhat:lightspeed_services_acm:0.1" \
      vendor="Red Hat, Inc." \
      summary="ACM recommendations powered by Red Hat Lightspeed" \
      io.k8s.display-name="ACM recommendations powered by Red Hat Lightspeed" \
      io.k8s.description="Based on lightspeed-services-ocp/ocp-rules-rhel9, \
this image runs OCP rules and provides recommendations for clusters in an ACM \
setup. Useful in disconnected environments where customers cannot send Insights \
Operator archives to Red Hat cloud."

ENTRYPOINT []
CMD ["python", "-m", "app.main"]
