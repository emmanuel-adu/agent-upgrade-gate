# Execution image for `ugate run --sandbox docker`.
# Containers are started with --network none, --cap-drop ALL, no-new-privileges, pid/memory/cpu
# limits and the runner's non-root uid; see src/upgrade_gate/sandbox.py.
# Pin by digest in production (docker pull python:3.12-slim && docker inspect --format '{{.RepoDigests}}').
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "pytest>=8,<9"
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /workspace
