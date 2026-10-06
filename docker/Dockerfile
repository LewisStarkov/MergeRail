# Build only this operator-owned runtime, never a repository's Dockerfile.
FROM ghcr.io/anomalyco/opencode@sha256:d654ecb68ae52ae3abcc56a2a07ff25647e33c38d5a7fc880c5ee20ee54c7d30
USER root
RUN apk add --no-cache python3 git nodejs npm
RUN npm install --global --ignore-scripts @openai/codex@0.159.2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3"]
