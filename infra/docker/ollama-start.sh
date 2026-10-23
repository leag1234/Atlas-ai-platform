#!/bin/sh
set -eu
rm -f /tmp/model-ready
ollama serve &
server_pid=$!
trap 'kill "$server_pid"; wait "$server_pid"' TERM INT EXIT
attempt=0
until ollama list >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 60 ] || exit 1
    sleep 1
done
printf '%s\n' 'Ollama ready; pulling the selected model. CPU mode works without a GPU but is slower and weaker.'
ollama pull "$ATLAS_LOCAL_MODEL"
touch /tmp/model-ready
printf '%s\n' 'Local model ready.'
wait "$server_pid"
