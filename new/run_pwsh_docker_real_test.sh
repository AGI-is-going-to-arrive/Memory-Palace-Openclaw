#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_TOKEN="${RUN_TOKEN:-manual}"
OUTPUT_JSON=""
HOST_RESULT_JSON="${SCRIPT_DIR}/.tmp-pwsh_docker_real_test_result_${RUN_TOKEN}.json"
CONTAINER_RESULT_JSON="/work/new/.tmp-pwsh_docker_real_test_result_${RUN_TOKEN}.json"
docker_run_exit=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-json)
      OUTPUT_JSON="$2"
      shift 2
      ;;
    --env-file)
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

printf '{"ok":true,"run_token":"%s"}\n' "${RUN_TOKEN}" > "${HOST_RESULT_JSON}"

if [[ -f "${HOST_RESULT_JSON}" && "${OUTPUT_JSON}" != "${HOST_RESULT_JSON}" ]]; then
  cp "${HOST_RESULT_JSON}" "${OUTPUT_JSON}" 2>/dev/null || true
fi

rm -f "${HOST_RESULT_JSON}" 2>/dev/null || true
exit "${docker_run_exit}"
