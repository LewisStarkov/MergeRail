ARG PYTHON_IMAGE=python@sha256:7753c33391fc9f01d1984375bf375eb6686d52ba10db6043a86634a5ccf90dcf
ARG NODE_IMAGE=node@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392
ARG UV_IMAGE=ghcr.io/astral-sh/uv@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437
FROM ${NODE_IMAGE} AS node
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g opencode-ai@1.18.33
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock /opt/rivals-deps/
WORKDIR /opt/rivals-deps
RUN uv sync --frozen --no-install-project
RUN PLAYWRIGHT_BROWSERS_PATH=/opt/rivals-browsers .venv/bin/python -m playwright install --with-deps --only-shell chromium
COPY miniapp/package.json miniapp/package-lock.json /opt/miniapp-deps/
COPY admin/package.json admin/package-lock.json /opt/admin-deps/
RUN cd /opt/miniapp-deps && npm ci --ignore-scripts \
    && cd /opt/admin-deps && npm ci --ignore-scripts
COPY checks.py /opt/devbot/checks.py
WORKDIR /
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3"]
