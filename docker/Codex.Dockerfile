FROM node:22-bookworm-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gh git openssh-client python3 ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @openai/codex@0.166.0

RUN mkdir -p /codex-home
