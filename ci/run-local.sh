#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

if [ "$#" -gt 1 ]; then
    echo "Usage: sh ci/run-local.sh [build|test|sast|deploy]" >&2
    exit 2
fi
case "${1:-}" in
    ""|build|test|sast|deploy) ;;
    *) echo "Unknown job: $1" >&2; exit 2 ;;
esac
job=${1:-}

if command -v act >/dev/null 2>&1; then
    act_bin=act
elif [ -f .tools/act.exe ]; then
    act_bin=./.tools/act.exe
else
    echo "Install act first: see CI.md" >&2
    exit 1
fi

docker build -f ci/Dockerfile.runner -t file-manager-ci-runner:local .

# Cache shallow copies of actions for act's offline mode.
mkdir -p .cache/actions
for name in cache upload-artifact download-artifact; do
    target=".cache/actions/actions-$name@v4"
    url="https://github.com/actions/$name"
    if [ -f "$target/action.yml" ]; then
        continue
    fi
    if [ -d "$target/.git" ]; then
        git -C "$target" remote set-url origin "$url"
        git -C "$target" fetch --depth 1 origin tag v4
        git -C "$target" checkout v4
    else
        git -c advice.detachedHead=false clone --depth 1 --branch v4 "$url" "$target"
    fi
done

# Secrets stay in this process's environment, never in command arguments/files.
new_password() {
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}
export CI_PGADMIN_PASSWORD="${CI_PGADMIN_PASSWORD:-$(new_password)}"
export CI_PGAPP_PASSWORD="${CI_PGAPP_PASSWORD:-$(new_password)}"
export ACTIONS_RUNTIME_URL=http://host.docker.internal:34567/

set -- push -W .github/workflows/main.yml \
    --var CI_RUNNER_IMAGE=file-manager-ci-runner:local \
    --secret CI_PGADMIN_PASSWORD --secret CI_PGAPP_PASSWORD \
    --artifact-server-addr 127.0.0.1 \
    --cache-server-addr 127.0.0.1 --cache-server-port 34568 \
    --cache-server-external-url http://host.docker.internal:34568 \
    --env "GITHUB_RUN_ID=$(date +%s)$$" --pull=false
if [ -n "$job" ]; then
    set -- "$@" -j "$job"
fi

# Keep Git Bash from rewriting Linux paths in act's Docker configuration.
MSYS_NO_PATHCONV=1 "$act_bin" "$@"
