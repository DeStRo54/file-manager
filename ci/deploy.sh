#!/bin/sh
set -eu
: "${CI_PGADMIN_PASSWORD:?Set CI_PGADMIN_PASSWORD}"
: "${CI_PGAPP_PASSWORD:?Set CI_PGAPP_PASSWORD}"
if [ "$CI_PGADMIN_PASSWORD" = "$CI_PGAPP_PASSWORD" ]; then
    echo "CI database passwords must be different" >&2
    exit 1
fi
# Empty env file keeps Compose from reading the developer's .env.
compose() {
    docker compose --env-file ci/runner.env -f compose.ci.yaml "$@"
}

keep=false
cleanup() {
    if [ "$keep" = false ]; then
        compose down --volumes --remove-orphans
    fi
}
trap cleanup 0

# Every demonstration starts with a clean, separate CI database.
compose down --volumes --remove-orphans
compose build --pull app
compose up -d --wait --wait-timeout 120

mkdir -p reports
docker image save -o reports/file-manager-image.tar file-manager-ci:local
gzip -f reports/file-manager-image.tar
printf 'http://%s/health\n' "$(compose port app 8080)" > reports/health-url.txt
cat reports/health-url.txt
compose ps

# Keep a successful local demo; GitHub and failed runs clean up automatically.
if [ "${ACT:-}" = true ]; then
    keep=true
fi
