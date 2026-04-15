#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

detect_default_platform() {
  case "$(uname -s 2>/dev/null || true)" in
    Linux) printf '%s\n' "linux" ;;
    Darwin) printf '%s\n' "macos" ;;
    MINGW*|MSYS*|CYGWIN*) printf '%s\n' "windows" ;;
    *) printf '%s\n' "macos" ;;
  esac
}

platform_input="${1:-$(detect_default_platform)}"
profile_input="${2:-b}"
target_file="${3:-${PROJECT_ROOT}/.env}"

platform="$(printf '%s' "${platform_input}" | tr '[:upper:]' '[:lower:]')"
profile="$(printf '%s' "${profile_input}" | tr '[:upper:]' '[:lower:]')"

case "${platform}" in
  macos|linux|windows|docker) ;;
  *)
    echo "Unsupported platform: ${platform}. Expected one of: macos | linux | windows | docker" >&2
    exit 2
    ;;
esac

case "${profile}" in
  a|b|c|d) ;;
  *)
    echo "Unsupported profile: ${profile}. Expected one of: a | b | c | d" >&2
    exit 2
    ;;
esac

base_env="${PROJECT_ROOT}/.env.example"
override_env="${PROJECT_ROOT}/deploy/profiles/${platform}/profile-${profile}.env"

create_temp_file() {
  local file_path="$1"
  local tmp_file
  tmp_file="$(mktemp "${TMPDIR:-/tmp}/${file_path##*/}.XXXXXX")"
  chmod 600 "${tmp_file}"
  printf '%s\n' "${tmp_file}"
}

set_env_value() {
  local file_path="$1"
  local key="$2"
  local value="$3"
  local tmp_file
  tmp_file="$(create_temp_file "${file_path}")"
  awk -v key="${key}" -v value="${value}" '
    BEGIN { replaced = 0 }
    $0 ~ ("^" key "=") {
      if (!replaced) {
        print key "=" value
        replaced = 1
      }
      next
    }
    { print }
    END {
      if (!replaced) {
        print key "=" value
      }
    }
  ' "${file_path}" > "${tmp_file}"
  mv "${tmp_file}" "${file_path}"
}

generate_random_mcp_api_key() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24 | tr -d '\r\n'
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(24))'
    return 0
  fi

  echo "Failed to generate MCP_API_KEY: neither openssl nor python3 is available." >&2
  return 1
}

normalize_env_file() {
  local file_path="$1"
  local tmp_file
  tmp_file="$(create_temp_file "${file_path}")"
  awk '{ sub(/\r$/, ""); print }' "${file_path}" > "${tmp_file}"
  mv "${tmp_file}" "${file_path}"
}

get_env_value() {
  local file_path="$1"
  local key="$2"
  awk -F= -v key="${key}" '$1 == key { value = substr($0, length($1) + 2) } END { print value }' "${file_path}"
}

resolve_windows_db_path() {
  local db_path="${PROJECT_ROOT}/demo.db"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -am "${db_path}"
    return 0
  fi
  case "${db_path}" in
    /mnt/[a-zA-Z]/*)
      printf '%s\n' "${db_path}" | sed -E 's#^/mnt/([a-zA-Z])/#\1:/#'
      return 0
      ;;
    /[a-zA-Z]/*)
      printf '%s\n' "${db_path}" | sed -E 's#^/([a-zA-Z])/#\1:/#'
      return 0
      ;;
  esac
  printf '%s\n' 'C:/memory_palace/demo.db'
}

dedupe_env_keys() {
  local file_path="$1"
  local key value
  while IFS= read -r key; do
    [[ -n "${key}" ]] || continue
    value="$(awk -F= -v key="${key}" '$1 == key { value = substr($0, length($1) + 2) } END { print value }' "${file_path}")"
    set_env_value "${file_path}" "${key}" "${value}"
  done < <(
    awk -F= '/^[A-Z0-9_]+=/{ count[$1]++ } END { for (key in count) if (count[key] > 1) print key }' "${file_path}" | sort
  )
}

find_profile_placeholders() {
  local file_path="$1"
  local profile_name="$2"
  awk -F= '
    /^[A-Z0-9_]+=/{
      key=$1
      value=tolower(substr($0, length($1) + 2))
      required=0
      if (key ~ /^(RETRIEVAL_EMBEDDING_API_BASE|RETRIEVAL_EMBEDDING_API_KEY|RETRIEVAL_EMBEDDING_MODEL|RETRIEVAL_RERANKER_API_BASE|RETRIEVAL_RERANKER_API_KEY|RETRIEVAL_RERANKER_MODEL)$/) {
        required=1
      } else if (profile == "d" && key ~ /^(LLM_API_BASE|LLM_API_KEY|LLM_MODEL_NAME|WRITE_GUARD_LLM_API_BASE|WRITE_GUARD_LLM_API_KEY|WRITE_GUARD_LLM_MODEL|COMPACT_GIST_LLM_API_BASE|COMPACT_GIST_LLM_API_KEY|COMPACT_GIST_LLM_MODEL|INTENT_LLM_API_BASE|INTENT_LLM_API_KEY|INTENT_LLM_MODEL)$/) {
        required=1
      }
      if (required) {
        if (index(value, "replace-with-your-") || index(value, "<your-") || index(value, "127.0.0.1:port") || index(value, "host.docker.internal:port") || index(value, "https://<") || index(value, "http://<")) {
          print key "=" substr($0, length($1) + 2)
        }
      }
    }
  ' profile="${profile_name}" "${file_path}"
}

if [[ ! -f "${base_env}" ]]; then
  echo "Missing base env template: ${base_env}" >&2
  exit 1
fi

if [[ ! -f "${override_env}" ]]; then
  echo "Missing profile template: ${override_env}" >&2
  exit 1
fi

cp "${base_env}" "${target_file}"
{
  echo
  echo "# -----------------------------------------------------------------------------"
  echo "# Appended profile overrides (${platform}/profile-${profile})"
  echo "# -----------------------------------------------------------------------------"
  cat "${override_env}"
} >> "${target_file}"
normalize_env_file "${target_file}"

if [[ "${platform}" == "macos" || "${platform}" == "linux" ]]; then
  if grep -Eq '^DATABASE_URL=sqlite\+aiosqlite:////Users/<your-user>/memory_palace/agent_memory\.db$' "${target_file}"; then
    db_path="${PROJECT_ROOT}/demo.db"
    set_env_value "${target_file}" "DATABASE_URL" "sqlite+aiosqlite:////${db_path#/}"
    echo "[auto-fill] DATABASE_URL set to ${db_path}"
  fi
  if grep -Eq '^DATABASE_URL=sqlite\+aiosqlite:////home/<your-user>/memory_palace/agent_memory\.db$' "${target_file}"; then
    db_path="${PROJECT_ROOT}/demo.db"
    set_env_value "${target_file}" "DATABASE_URL" "sqlite+aiosqlite:////${db_path#/}"
    echo "[auto-fill] DATABASE_URL set to ${db_path}"
  fi
elif [[ "${platform}" == "windows" ]]; then
  if grep -Eq '^DATABASE_URL=sqlite\+aiosqlite:///C:/memory_palace/agent_memory\.db$' "${target_file}"; then
    if db_path="$(resolve_windows_db_path)"; then
      set_env_value "${target_file}" "DATABASE_URL" "sqlite+aiosqlite:///${db_path}"
      echo "[auto-fill] DATABASE_URL set to ${db_path}"
    fi
  fi
fi

if [[ "${platform}" == "docker" ]]; then
  current_mcp_api_key="$(get_env_value "${target_file}" "MCP_API_KEY")"
  if [[ -z "${current_mcp_api_key}" ]]; then
    generated_mcp_api_key="$(generate_random_mcp_api_key)"
    set_env_value "${target_file}" "MCP_API_KEY" "${generated_mcp_api_key}"
    echo "[auto-fill] MCP_API_KEY generated for docker profile"
  fi
fi

dedupe_env_keys "${target_file}"

if [[ "${profile}" == "c" || "${profile}" == "d" ]]; then
  placeholder_lines="$(find_profile_placeholders "${target_file}" "${profile}")"
  if [[ -n "${placeholder_lines}" ]]; then
    {
      echo "Generated ${target_file}, but Profile ${profile^^} still contains placeholder provider values."
      echo "Fill in real provider settings or use onboarding before treating this profile as ready."
      printf '%s\n' "${placeholder_lines}"
    } >&2
    exit 3
  fi
fi

echo "Generated ${target_file} from ${override_env}"
