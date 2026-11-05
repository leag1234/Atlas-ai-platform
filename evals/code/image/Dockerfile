# Benchmark image: the agent runs here (Codex configurations) and so do the hidden checks (without network).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && git config --system user.name bench && git config --system user.email bench@example.invalid \
    && git config --system --add safe.directory '*'
ENV PYTHONDONTWRITEBYTECODE=1
