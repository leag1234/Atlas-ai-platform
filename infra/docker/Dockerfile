FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app:/app/services/model-gateway HF_HOME=/models
RUN apt-get update \
    && apt-get install -y --no-install-recommends file \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY infra/docker/entrypoint.py /app/entrypoint.py
COPY packages packages
COPY services services
COPY contracts contracts
COPY config/atlas-policy.json config/atlas-policy.json
COPY prompts prompts
RUN mkdir -p /app/BRAIN /models
ENTRYPOINT ["python", "/app/entrypoint.py"]

# Project Work uses the existing networkless terminal-image workers. Only the
# explicitly selected project target contains the host Docker client.
FROM runtime AS project
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker:28-cli /usr/local/bin/docker /usr/local/bin/docker

FROM runtime AS default
