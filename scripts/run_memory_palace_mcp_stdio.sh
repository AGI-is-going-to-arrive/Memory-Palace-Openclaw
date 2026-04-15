#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -d "${SCRIPT_DIR}/../backend" ]]; then
  PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
  BACKEND_DIR="${PROJECT_ROOT}/backend"
elif [[ -d "${SCRIPT_DIR}/../../backend" ]]; then
  PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
  BACKEND_DIR="${PROJECT_ROOT}/backend"
else
  echo "Could not resolve backend directory beside stdio wrapper." >&2
  exit 1
fi

trim_whitespace() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
}

is_blocked_runtime_env_key() {
  local key="$1"
  case "${key}" in
    PATH|LD_PRELOAD|DYLD_INSERT_LIBRARIES|PYTHONPATH|PYTHONHOME|VIRTUAL_ENV|BASH_ENV|ENV|SHELLOPTS|HOME|TMPDIR|SHELL)
      return 0
      ;;
  esac
  return 1
}

is_isolated_runtime_env_key() {
  local key="$1"
  case "${key}" in
    DATABASE_URL|MCP_API_KEY|OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE|OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED)
      return 0
      ;;
    OPENAI_*|LLM_*|SMART_EXTRACTION_LLM_*|WRITE_GUARD_LLM_*|COMPACT_GIST_LLM_*|RETRIEVAL_EMBEDDING_*|RETRIEVAL_RERANKER_*|ROUTER_*|EMBEDDING_PROVIDER_*)
      return 0
      ;;
  esac
  return 1
}

clear_isolated_runtime_env() {
  local key
  while IFS='=' read -r key _; do
    [[ -n "${key}" ]] || continue
    if is_isolated_runtime_env_key "${key}"; then
      unset "${key}"
    fi
  done < <(env)
}

load_env_file() {
  local env_file="$1"
  [[ -f "${env_file}" ]] || return 0

  local line normalized key value first_char last_char
  while IFS= read -r line || [[ -n "${line}" ]]; do
    normalized="${line%$'\r'}"
    normalized="${normalized#$'\xef\xbb\xbf'}"
    if [[ "${normalized}" == export[[:space:]]* ]]; then
      normalized="${normalized#export }"
      normalized="$(trim_whitespace "${normalized}")"
    fi
    [[ "${normalized}" == *"="* ]] || continue

    key="$(trim_whitespace "${normalized%%=*}")"
    [[ -n "${key}" ]] || continue
    [[ "${key:0:1}" == "#" ]] && continue
    if [[ ! "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      echo "Skipping invalid env key in ${env_file}: ${key}" >&2
      continue
    fi
    if is_blocked_runtime_env_key "${key}"; then
      echo "Skipping blocked env key in ${env_file}: ${key}" >&2
      continue
    fi

    value="$(trim_whitespace "${normalized#*=}")"
    if [[ ${#value} -ge 2 ]]; then
      first_char="${value:0:1}"
      last_char="${value: -1}"
      if [[ "${first_char}" == "${last_char}" && ( "${first_char}" == "\"" || "${first_char}" == "'" ) ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi

    printf -v "${key}" '%s' "${value}"
    export "${key}"
  done < "${env_file}"
}

if [[ -n "${OPENCLAW_MEMORY_PALACE_ENV_FILE:-}" ]]; then
  clear_isolated_runtime_env
  load_env_file "${OPENCLAW_MEMORY_PALACE_ENV_FILE}"
fi

VENV_PYTHON="${OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON:-${BACKEND_DIR}/.venv/bin/python}"
DB_PATH="${OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT:-${BACKEND_DIR}}/data/memory-palace.db"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Missing backend virtualenv python: ${VENV_PYTHON}" >&2
  exit 1
fi

cd "${BACKEND_DIR}"

export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:////${DB_PATH#/}}"
export RETRIEVAL_REMOTE_TIMEOUT_SEC="${RETRIEVAL_REMOTE_TIMEOUT_SEC:-1}"
# OpenClaw product default favors lower end-to-end latency while the backend
# benchmark baseline continues to evaluate the quality-first top-48 setting.
export RETRIEVAL_RERANK_TOP_N="${RETRIEVAL_RERANK_TOP_N:-12}"

# WAL mode is recommended for concurrent access (auto-recall + auto-capture).
# Default to WAL when not explicitly set, since the setup path now enables it.
export RUNTIME_WRITE_WAL_ENABLED="${RUNTIME_WRITE_WAL_ENABLED:-true}"
export RUNTIME_WRITE_JOURNAL_MODE="${RUNTIME_WRITE_JOURNAL_MODE:-wal}"

exec "${VENV_PYTHON}" mcp_server.py
