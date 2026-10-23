#!/bin/sh
# Docker CLI tools for the Linux Deploy container. No extra Docker daemon.
set -eu
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    exit 0
fi
# act uses linux/amd64; GitHub's ubuntu-latest runner is also x86_64.
curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-29.7.2.tgz \
    -o /tmp/docker-cli.tgz
tar -xzf /tmp/docker-cli.tgz -C /usr/local/bin --strip-components=1 docker/docker
rm /tmp/docker-cli.tgz
mkdir -p /usr/local/libexec/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/download/v5.4.0/docker-compose-linux-x86_64 \
    -o /usr/local/libexec/docker/cli-plugins/docker-compose
curl -fsSL https://github.com/docker/buildx/releases/download/v0.36.1/buildx-v0.36.1.linux-amd64 \
    -o /usr/local/libexec/docker/cli-plugins/docker-buildx
chmod +x /usr/local/libexec/docker/cli-plugins/docker-*
docker compose version
