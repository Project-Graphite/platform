#!/bin/sh
set -eu

refuse() {
    echo "The judge will not start: $1" >&2
    exit 1
}

info() {
    docker info --format "$1"
}

info '{{.ID}}' > /dev/null || refuse "no Docker daemon answers at $DOCKER_HOST"

case "$(info '{{json .SecurityOptions}}')" in
    *name=rootless*) ;;
    *) refuse "$DOCKER_HOST is not a rootless daemon" ;;
esac

for limit in MemoryLimit SwapLimit CPUCfsQuota CPUSet PidsLimit; do
    [ "$(info "{{.$limit}}")" = true ] || refuse "the daemon cannot enforce $limit, so delegate that cgroup controller to its user"
done

touch "$JOBS_DIR/.writable" 2> /dev/null || refuse "$JOBS_DIR is not writable by $(id -u)"
rm -f "$JOBS_DIR/.writable"

for language in cpp python; do
    echo "Building maat-$language"
    DOCKER_BUILDKIT=0 docker build -q -t "maat-$language" - < "/app/sandbox-images/$language.Dockerfile"
done

exec node src/server.js
