FROM node:22-bookworm-slim AS mermaid
WORKDIR /parser
COPY package*.json ./
RUN npm ci --ignore-scripts --omit=dev --no-audit --no-fund

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app:/app/services/model-gateway HF_HOME=/models
COPY requirements-dev.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY infra/docker/entrypoint.py /app/entrypoint.py
COPY packages packages
COPY services services
COPY --from=mermaid /usr/local/bin/node /usr/local/bin/node
COPY --from=mermaid /parser/node_modules node_modules
COPY contracts contracts
COPY config/atlas-policy.json config/atlas-policy.json
COPY prompts prompts
RUN mkdir -p /app/BRAIN /models
ENTRYPOINT ["python", "/app/entrypoint.py"]
