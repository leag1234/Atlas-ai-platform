FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app:/app/services/model-gateway HF_HOME=/models
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
