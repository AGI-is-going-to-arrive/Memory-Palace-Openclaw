#!/usr/bin/env bash
set -euo pipefail

ROOT_BASHPID="${BASHPID:-$$}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNTIME_ENV_FILE="${RUNTIME_ENV_FILE:-${REPO_ROOT}/.env}"
MEMORY_PALACE_DOCKER_ENV_FILE="${MEMORY_PALACE_DOCKER_ENV_FILE:-${REPO_ROOT}/.env.docker}"
LOCK_DIR="${REPO_ROOT}/.tmp/memory-palace-post-change-checks"
PORT_LOCK_DIR="${REPO_ROOT}/.tmp/memory-palace-port-locks"

if [[ "${BASHPID:-$$}" != "${ROOT_BASHPID}" ]]; then
  return 0 2>/dev/null || exit 0
fi

mkdir -p "${LOCK_DIR}" "${PORT_LOCK_DIR}"

if [[ -e "${LOCK_DIR}/active.lock" ]]; then
  echo "Another run_post_change_checks.sh process is already active for this workspace."
  exit 1
fi
touch "${LOCK_DIR}/active.lock"
trap 'rm -f "${LOCK_DIR}/active.lock"' EXIT

try_acquire_path_lock() {
  local _path="$1"
  mkdir -p "$(dirname "${_path}")"
  : > "${_path}"
}

reserve_exact_port_if_available() {
  local _port="$1"
  local _lock_file="${PORT_LOCK_DIR}/${_port}.lock"
  try_acquire_path_lock "${_lock_file}"
}

append_review_record() {
  local _label="$1"
  local _status="$2"
  echo "workspace.${_label}: ${_status}"
}

run_review_snapshots_http_smoke_gate() {
  local smoke_modes="local"
  local report_file="${REPO_ROOT}/.tmp/review_snapshots_http_smoke.md"
  echo "local-only mode to avoid duplicating compose lifecycle"
  python scripts/review_snapshots_http_smoke.py --modes "${smoke_modes}" --report "${report_file}"
  echo "workspace.review_snapshots_http_smoke"
  append_review_record "review_snapshots_http_smoke" "PASS"
}

run_windows_equivalent_pwsh_docker_gate() {
  local result_json="${REPO_ROOT}/.tmp/windows-equivalent.json"
  local pwsh_exit_code=0
  local status="SKIP"

  if (cd "${REPO_ROOT}" && bash new/run_pwsh_docker_real_test.sh --env-file "${RUNTIME_ENV_FILE}" --output-json "${result_json}"); then
    status="PASS"
  else
    pwsh_exit_code=$?
    status="FAIL"
  fi

  if [[ "${status}" == "FAIL" ]]; then
    return "${pwsh_exit_code}"
  fi

  # append_review_record()
}
