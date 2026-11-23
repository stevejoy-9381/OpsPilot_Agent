#!/usr/bin/env bash
# Fail if any Compose file references the Docker control socket, except inside
# the explicitly allowlisted "broker" service.
#
# Verified Compose files include:
#   - infra/docker-compose.yml (core PostgreSQL & Redis)
#   - infra/docker-compose.lab.yml (gateway, orders, inventory, lab-postgres, lab-redis)
#
# Lab microservices simulate deployments strictly in-process via authenticated
# admin endpoints over `lab-admin`; mounting the Docker control socket into any lab
# service is strictly forbidden.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
allowlisted_service="broker"
socket_path="/var/run/docker.sock"

mapfile -t compose_files < <(
  find "$repo_root" \
    \( -name .git -o -name node_modules -o -name .venv \) -prune \
    -o -type f \( -name '*compose*.yml' -o -name '*compose*.yaml' \) -print | sort
)

if (( ${#compose_files[@]} == 0 )); then
  echo "check_no_docker_socket: no Compose files found" >&2
  exit 1
fi

violations=0
for file in "${compose_files[@]}"; do
  while IFS=$'\t' read -r line_no service line; do
    [[ -z "${line_no:-}" ]] && continue
    if [[ "$service" == "$allowlisted_service" ]]; then
      continue
    fi
    echo "ERROR: Docker socket reference outside allowlisted '$allowlisted_service' service:" >&2
    echo "  ${file#"$repo_root"/}:$line_no: $line" >&2
    violations=$((violations + 1))
  done < <(
    awk -v needle="$socket_path" '
      /^services:[[:space:]]*$/ { in_services = 1; next }
      in_services && /^[^[:space:]#]/ { in_services = 0 }
      in_services && /^  [A-Za-z0-9_.-]+:[[:space:]]*$/ {
        service = $1
        sub(/:$/, "", service)
      }
      index($0, needle) { print FNR "\t" service "\t" $0 }
    ' "$file"
  )
done

if (( violations > 0 )); then
  echo "check_no_docker_socket: FAILED ($violations reference(s))" >&2
  exit 1
fi

echo "check_no_docker_socket: OK (0 references in ${#compose_files[@]} Compose file(s))"
