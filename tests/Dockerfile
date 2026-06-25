FROM registry.access.redhat.com/ubi9-minimal:latest

ENV HOME=/tests \
    VENV=/tests-venv \
    PYTHONUNBUFFERED=1 \
    PATH="/tests-venv/bin:$PATH"

WORKDIR $HOME

# curl-minimal and tar are required to download the oc client from the cluster at runtime.
# curl-minimal is used instead of curl to avoid conflicts with ubi9-minimal's base packages.
# oc is NOT baked into this image — it is downloaded from the target OCP cluster's
# downloads route to guarantee version alignment:
#   curl -kL https://downloads-openshift-console.apps.<cluster>/amd64/linux/oc.tar.gz | tar xz -C /usr/local/bin
RUN microdnf install --nodocs -y \
        python3.12 \
        curl-minimal \
        tar \
        jq \
        git \
    && python3.12 -m venv $VENV \
    && pip install --no-cache-dir -U pip setuptools wheel \
    && microdnf clean all

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

USER 1001
