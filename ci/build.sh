#!/bin/sh
set -eu
python -m compileall -q app db main.py
mkdir -p dist
# Pass only source files; never include the working .env, data or virtualenv.
tar --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='.env*' --exclude='.secrets*' --exclude='*.dump' --exclude='*.backup' \
    -czf dist/source.tar.gz \
    app db tests ci main.py requirements.txt Dockerfile .dockerignore .semgrep.yml compose.ci.yaml
