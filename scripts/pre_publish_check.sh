#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MODE="scan"
REPORT_PATH=""
PROFILE_SMOKE_MODES="local,docker"
REVIEW_SMOKE_MODES="local,docker"
PROFILE_SMOKE_MODEL_ENV=""
SKIP_BACKEND_TESTS=0
SKIP_PLUGIN_TESTS=0
ENABLE_LIVE_BENCHMARK=0
ENABLE_WINDOWS_NATIVE_VALIDATION=0
SKIP_ONBOARDING_APPLY_VALIDATE=0
SKIP_PROFILE_SMOKE=0
SKIP_PHASE45=0
SKIP_REVIEW_SMOKE=0
SKIP_FRONTEND_TESTS=0
SKIP_FRONTEND_E2E=0
ENABLE_CURRENT_HOST_STRICT_UI=0
SKIP_CURRENT_HOST_STRICT_UI=0

EXIT_CODE=0
WARNINGS=0
RELEASE_STEP_STDOUT_TAIL_LINES="${RELEASE_STEP_STDOUT_TAIL_LINES:-80}"
RELEASE_STEP_TIMEOUT_DEFAULT="${RELEASE_STEP_TIMEOUT_DEFAULT:-0}"
RELEASE_STEP_TIMEOUT_BACKEND_PYTEST="${RELEASE_STEP_TIMEOUT_BACKEND_PYTEST:-1800}"
RELEASE_STEP_TIMEOUT_SCRIPT_PYTEST="${RELEASE_STEP_TIMEOUT_SCRIPT_PYTEST:-1800}"
RELEASE_STEP_TIMEOUT_MCP_STDIO_E2E="${RELEASE_STEP_TIMEOUT_MCP_STDIO_E2E:-600}"
RELEASE_STEP_TIMEOUT_PLUGIN_BUN_TESTS="${RELEASE_STEP_TIMEOUT_PLUGIN_BUN_TESTS:-900}"
RELEASE_STEP_TIMEOUT_PACKAGE_INSTALL="${RELEASE_STEP_TIMEOUT_PACKAGE_INSTALL:-1800}"
RELEASE_STEP_TIMEOUT_ONBOARDING_APPLY_VALIDATE="${RELEASE_STEP_TIMEOUT_ONBOARDING_APPLY_VALIDATE:-1800}"
RELEASE_STEP_TIMEOUT_PROFILE_SMOKE="${RELEASE_STEP_TIMEOUT_PROFILE_SMOKE:-5400}"
RELEASE_STEP_TIMEOUT_VISUAL_BENCHMARK="${RELEASE_STEP_TIMEOUT_VISUAL_BENCHMARK:-0}"
RELEASE_STEP_TIMEOUT_REVIEW_SMOKE="${RELEASE_STEP_TIMEOUT_REVIEW_SMOKE:-1800}"
RELEASE_STEP_TIMEOUT_FRONTEND_TESTS="${RELEASE_STEP_TIMEOUT_FRONTEND_TESTS:-900}"
RELEASE_STEP_TIMEOUT_FRONTEND_E2E="${RELEASE_STEP_TIMEOUT_FRONTEND_E2E:-1800}"
RELEASE_STEP_TIMEOUT_CURRENT_HOST_STRICT_UI="${RELEASE_STEP_TIMEOUT_CURRENT_HOST_STRICT_UI:-2400}"
RELEASE_STEP_FRONTEND_E2E_API_PORT="${RELEASE_STEP_FRONTEND_E2E_API_PORT:-18081}"
RELEASE_STEP_FRONTEND_E2E_UI_PORT="${RELEASE_STEP_FRONTEND_E2E_UI_PORT:-4174}"
VISUAL_BENCHMARK_TIMEOUT_BASE_SECONDS="${VISUAL_BENCHMARK_TIMEOUT_BASE_SECONDS:-180}"
VISUAL_BENCHMARK_TIMEOUT_PER_PROFILE_SECONDS="${VISUAL_BENCHMARK_TIMEOUT_PER_PROFILE_SECONDS:-120}"
VISUAL_BENCHMARK_TIMEOUT_PER_CASE_SECONDS="${VISUAL_BENCHMARK_TIMEOUT_PER_CASE_SECONDS:-12}"
VISUAL_BENCHMARK_TIMEOUT_FLOOR_SECONDS="${VISUAL_BENCHMARK_TIMEOUT_FLOOR_SECONDS:-900}"
VISUAL_BENCHMARK_TIMEOUT_CEILING_SECONDS="${VISUAL_BENCHMARK_TIMEOUT_CEILING_SECONDS:-7200}"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/pre_publish_check.sh
  bash scripts/pre_publish_check.sh --release-gate [--report <path>]
                                     [--profile-smoke-modes <modes>]
                                     [--profile-smoke-model-env <path>]
                                     [--review-smoke-modes <modes>]
                                     [--skip-backend-tests]
                                     [--skip-plugin-tests]
                                     [--enable-live-benchmark]
                                     [--enable-windows-native-validation]
                                     [--skip-onboarding-apply-validate]
                                     [--skip-profile-smoke]
                                     [--skip-phase45]
                                     [--skip-review-smoke]
                                     [--skip-frontend-tests]
                                     [--skip-frontend-e2e]
                                     [--enable-current-host-strict-ui]
                                     [--skip-current-host-strict-ui]

Modes:
  default           Only run the original local artifact + secret/path hygiene scan.
  --release-gate    Run the hygiene scan plus backend/plugin/frontend tests and smoke suites,
                    then write a Markdown report.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-gate)
      MODE="release-gate"
      shift
      ;;
    --report)
      REPORT_PATH="${2:-}"
      shift 2
      ;;
    --profile-smoke-modes)
      PROFILE_SMOKE_MODES="${2:-}"
      shift 2
      ;;
    --profile-smoke-model-env)
      PROFILE_SMOKE_MODEL_ENV="${2:-}"
      shift 2
      ;;
    --review-smoke-modes)
      REVIEW_SMOKE_MODES="${2:-}"
      shift 2
      ;;
    --skip-backend-tests)
      SKIP_BACKEND_TESTS=1
      shift
      ;;
    --skip-plugin-tests)
      SKIP_PLUGIN_TESTS=1
      shift
      ;;
    --enable-live-benchmark)
      ENABLE_LIVE_BENCHMARK=1
      shift
      ;;
    --enable-windows-native-validation)
      ENABLE_WINDOWS_NATIVE_VALIDATION=1
      shift
      ;;
    --skip-onboarding-apply-validate)
      SKIP_ONBOARDING_APPLY_VALIDATE=1
      shift
      ;;
    --skip-profile-smoke)
      SKIP_PROFILE_SMOKE=1
      shift
      ;;
    --skip-phase45)
      SKIP_PHASE45=1
      shift
      ;;
    --skip-review-smoke)
      SKIP_REVIEW_SMOKE=1
      shift
      ;;
    --skip-frontend-tests)
      SKIP_FRONTEND_TESTS=1
      shift
      ;;
    --skip-frontend-e2e)
      SKIP_FRONTEND_E2E=1
      shift
      ;;
    --enable-current-host-strict-ui)
      ENABLE_CURRENT_HOST_STRICT_UI=1
      shift
      ;;
    --skip-current-host-strict-ui)
      SKIP_CURRENT_HOST_STRICT_UI=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${ENABLE_LIVE_BENCHMARK}" -ne 1 ]]; then
  case "${RELEASE_GATE_ENABLE_LIVE_BENCHMARK:-${OPENCLAW_ENABLE_LIVE_BENCHMARK:-0}}" in
    1|true|TRUE|yes|YES|on|ON)
      ENABLE_LIVE_BENCHMARK=1
      ;;
  esac
fi

if [[ "${ENABLE_WINDOWS_NATIVE_VALIDATION}" -ne 1 ]]; then
  case "${RELEASE_GATE_ENABLE_WINDOWS_NATIVE_VALIDATION:-${OPENCLAW_ENABLE_WINDOWS_NATIVE_VALIDATION:-0}}" in
    1|true|TRUE|yes|YES|on|ON)
      ENABLE_WINDOWS_NATIVE_VALIDATION=1
      ;;
  esac
fi

if [[ "${ENABLE_CURRENT_HOST_STRICT_UI}" -eq 1 && "${SKIP_CURRENT_HOST_STRICT_UI}" -eq 1 ]]; then
  echo "Cannot combine --enable-current-host-strict-ui with --skip-current-host-strict-ui." >&2
  exit 2
fi

print_section() {
  printf "\n[%s]\n" "$1"
}

fail() {
  echo "FAIL: $*"
  EXIT_CODE=1
}

warn() {
  echo "WARN: $*"
  WARNINGS=$((WARNINGS + 1))
}

pass() {
  echo "PASS: $*"
}

resolve_python_bin() {
  local preferred="$1"
  if [[ -x "${preferred}" ]]; then
    printf '%s\n' "${preferred}"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

resolve_python_from_venv() {
  local venv_root="$1"
  local -a candidates=(
    "${venv_root}/bin/python"
    "${venv_root}/Scripts/python.exe"
    "${venv_root}/Scripts/python"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

resolve_bun_bin() {
  if command -v bun >/dev/null 2>&1; then
    printf '%s\n' "bun"
    return 0
  fi
  if command -v npx >/dev/null 2>&1; then
    printf '%s\n' "npx --yes bun"
    return 0
  fi
  return 1
}

shell_quote() {
  printf '%q' "$1"
}

is_wsl_host() {
  [[ -n "${WSL_DISTRO_NAME:-}" ]] && return 0
  grep -qi microsoft /proc/version 2>/dev/null
}

to_windows_path_if_needed() {
  local raw_path="$1"
  if command -v wslpath >/dev/null 2>&1 && [[ "${raw_path}" == /* ]]; then
    wslpath -w "${raw_path}"
    return 0
  fi
  printf '%s\n' "${raw_path}"
}

escape_powershell_single_quotes() {
  printf '%s' "${1//\'/\'\'}"
}

build_powershell_python_script_command() {
  local python_path="$1"
  local workdir="$2"
  shift 2

  if ! is_wsl_host || ! command -v powershell.exe >/dev/null 2>&1; then
    return 1
  fi

  local win_python
  win_python="$(to_windows_path_if_needed "${python_path}")"
  local win_workdir
  win_workdir="$(to_windows_path_if_needed "${workdir}")"
  local ps_command
  ps_command="Set-Location -LiteralPath '$(escape_powershell_single_quotes "${win_workdir}")'; & '$(escape_powershell_single_quotes "${win_python}")'"

  local arg normalized_arg
  for arg in "$@"; do
    normalized_arg="${arg}"
    if [[ "${normalized_arg}" == /* ]]; then
      normalized_arg="$(to_windows_path_if_needed "${normalized_arg}")"
    fi
    ps_command+=" '$(escape_powershell_single_quotes "${normalized_arg}")'"
  done

  printf 'powershell.exe -NoProfile -Command %q\n' "${ps_command}"
}

normalize_positive_int() {
  local value="${1:-}"
  local fallback="${2:-1}"
  if [[ "${value}" =~ ^[0-9]+$ ]] && [[ "${value}" -gt 0 ]]; then
    printf '%s\n' "${value}"
    return 0
  fi
  printf '%s\n' "${fallback}"
}

count_csv_items() {
  local raw="${1:-}"
  local count=0
  local item
  IFS=',' read -r -a items <<< "${raw}"
  for item in "${items[@]}"; do
    item="${item//[[:space:]]/}"
    [[ -n "${item}" ]] || continue
    count=$((count + 1))
  done
  if [[ "${count}" -le 0 ]]; then
    count=1
  fi
  printf '%s\n' "${count}"
}

compute_visual_benchmark_timeout_seconds() {
  if [[ "${RELEASE_STEP_TIMEOUT_VISUAL_BENCHMARK}" =~ ^[0-9]+$ ]] \
    && [[ "${RELEASE_STEP_TIMEOUT_VISUAL_BENCHMARK}" -gt 0 ]]; then
    printf '%s\n' "${RELEASE_STEP_TIMEOUT_VISUAL_BENCHMARK}"
    return 0
  fi

  local profiles_csv="${1:-a,b}"
  local case_limit
  case_limit="$(normalize_positive_int "${2:-64}" 64)"
  local max_workers
  max_workers="$(normalize_positive_int "${3:-1}" 1)"

  local profiles_count
  profiles_count="$(count_csv_items "${profiles_csv}")"
  local base_seconds
  base_seconds="$(normalize_positive_int "${VISUAL_BENCHMARK_TIMEOUT_BASE_SECONDS}" 180)"
  local per_profile_seconds
  per_profile_seconds="$(normalize_positive_int "${VISUAL_BENCHMARK_TIMEOUT_PER_PROFILE_SECONDS}" 120)"
  local per_case_seconds
  per_case_seconds="$(normalize_positive_int "${VISUAL_BENCHMARK_TIMEOUT_PER_CASE_SECONDS}" 12)"
  local floor_seconds
  floor_seconds="$(normalize_positive_int "${VISUAL_BENCHMARK_TIMEOUT_FLOOR_SECONDS}" 900)"
  local ceiling_seconds
  ceiling_seconds="$(normalize_positive_int "${VISUAL_BENCHMARK_TIMEOUT_CEILING_SECONDS}" 7200)"

  local total_cases=$((profiles_count * case_limit))
  local effective_case_batches=$(((total_cases + max_workers - 1) / max_workers))
  local computed=$((base_seconds + profiles_count * per_profile_seconds + effective_case_batches * per_case_seconds))

  if [[ "${computed}" -lt "${floor_seconds}" ]]; then
    computed="${floor_seconds}"
  fi
  if [[ "${ceiling_seconds}" -gt 0 ]] && [[ "${computed}" -gt "${ceiling_seconds}" ]]; then
    computed="${ceiling_seconds}"
  fi
  printf '%s\n' "${computed}"
}

check_local_artifacts() {
  local -a paths=(
    ".env"
    ".env.docker"
    ".venv"
    ".claude"
    ".tmp"
    "demo.db"
    "snapshots"
    "backend/backend.log"
    "frontend/frontend.log"
    "backend/.pytest_cache"
    "backend/tests/benchmark/.real_profile_cache"
    "frontend/node_modules"
    "frontend/dist"
  )
  local -a glob_paths=(
    "backend/*.db"
    "backend/*.sqlite"
    "backend/*.sqlite3"
  )

  local found_any=0
  local path
  for path in "${paths[@]}"; do
    if [[ -e "${path}" ]]; then
      warn "本地文件存在（上传前建议移除或确认未纳入提交）: ${path}"
      found_any=1
    fi
  done

  local pattern match
  for pattern in "${glob_paths[@]}"; do
    while IFS= read -r match; do
      [[ -n "${match}" ]] || continue
      warn "本地文件存在（上传前建议移除或确认未纳入提交）: ${match}"
      found_any=1
    done < <(compgen -G "${pattern}" || true)
  done

  if [[ "${found_any}" -eq 0 ]]; then
    pass "未发现高风险本地产物目录"
  fi
}

check_tracked_forbidden_paths() {
  local -a pathspecs=(
    ".env"
    ".env.docker"
    ".venv"
    ".claude"
    ".tmp"
    "demo.db"
    "snapshots"
    "backend/backend.log"
    "frontend/frontend.log"
    "backend/.pytest_cache"
    "backend/tests/benchmark/.real_profile_cache"
    "frontend/node_modules"
    "frontend/dist"
    "backend/*.db"
    "backend/*.sqlite"
    "backend/*.sqlite3"
  )

  local hit=0
  local tracked
  while IFS= read -r tracked; do
    [[ -n "${tracked}" ]] || continue
    fail "以下敏感/本地产物已被跟踪，请先移出版本库: ${tracked}"
    hit=1
  done < <(git ls-files -- "${pathspecs[@]}" || true)

  if [[ "${hit}" -eq 0 ]]; then
    pass "敏感本地产物未被跟踪"
  fi
}

collect_existing_tracked_files() {
  local file
  while IFS= read -r -d '' file; do
    if [[ -f "${file}" ]]; then
      printf '%s\0' "${file}"
    fi
  done < <(git ls-files -z)
}

is_scan_target_excluded() {
  local scan_key="$1"
  local file="$2"

  case "${scan_key}:${file}" in
    secret_scan:scripts/pre_publish_check.sh)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

collect_scannable_tracked_files() {
  local scan_key="$1"
  local file
  while IFS= read -r -d '' file; do
    if is_scan_target_excluded "${scan_key}" "${file}"; then
      continue
    fi
    printf '%s\0' "${file}"
  done < <(collect_existing_tracked_files)
}

scan_tracked_files() {
  local scan_key="$1"
  local label="$2"
  local regex="$3"

  local -a hits=()
  while IFS= read -r line; do
    [[ -n "${line}" ]] && hits+=("${line}")
  done < <(
    collect_scannable_tracked_files "${scan_key}" \
      | xargs -0 rg -l -n --no-messages "${regex}" 2>/dev/null \
      | sort -u || true
  )

  if [[ "${#hits[@]}" -gt 0 ]]; then
    fail "${label} 命中以下文件："
    printf '  - %s\n' "${hits[@]}"
  else
    pass "${label} 未命中"
  fi
}

scan_tracked_files_in_paths() {
  local scan_key="$1"
  local label="$2"
  local regex="$3"
  shift 3
  local -a pathspecs=("$@")
  local -a hits=()
  local file

  while IFS= read -r -d '' file; do
    if is_scan_target_excluded "${scan_key}" "${file}"; then
      continue
    fi
    [[ -f "${file}" ]] || continue
    hits+=("${file}")
  done < <(
    git ls-files -z -- "${pathspecs[@]}" \
      | xargs -0 rg -l -n --no-messages "${regex}" 2>/dev/null \
      | sort -u \
      | tr '\n' '\0' || true
  )

  if [[ "${#hits[@]}" -gt 0 ]]; then
    fail "${label} 命中以下文件："
    printf '  - %s\n' "${hits[@]}"
  else
    pass "${label} 未命中"
  fi
}

check_env_example_api_keys() {
  if [[ ! -f ".env.example" ]]; then
    fail "缺少 .env.example"
    return
  fi

  local -a hits=()
  local line
  local normalized
  local line_number=0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line_number=$((line_number + 1))
    normalized="${line%$'\r'}"
    [[ "${normalized}" =~ ^[A-Z0-9_]*API_KEY= ]] || continue
    if [[ "${normalized}" != *= ]]; then
      hits+=("${line_number}:${normalized}")
    fi
  done < ".env.example"

  if [[ "${#hits[@]}" -gt 0 ]]; then
    fail ".env.example 中发现非空 API_KEY，请改为空值占位"
    printf '  - %s\n' "${hits[@]}"
  else
    pass ".env.example 的 API_KEY 均为空占位"
  fi
}

run_scan_mode() {
  print_section "1) 本地敏感产物检查"
  check_local_artifacts

  print_section "2) Git 跟踪状态检查"
  check_tracked_forbidden_paths

  print_section "3) 密钥模式扫描（仅扫描已跟踪文件）"
  scan_tracked_files \
    "secret_scan" \
    "密钥/凭证模式" \
    'BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{35}|-----BEGIN PGP PRIVATE KEY BLOCK-----'

  CURRENT_USER="$(id -un 2>/dev/null || true)"
  if [[ -n "${CURRENT_USER}" ]]; then
    print_section "4) 个人路径泄露扫描（仅扫描已跟踪文件）"
    scan_tracked_files \
      "personal_path_scan" \
      "个人绝对路径（${CURRENT_USER}）" \
      "/Users/${CURRENT_USER}|C:\\\\Users\\\\${CURRENT_USER}|file:///Users/${CURRENT_USER}"
  fi

  print_section "4b) 通用个人路径泄露扫描（docs/README，仅扫描已跟踪文件）"
  scan_tracked_files_in_paths \
    "personal_path_scan" \
    "通用个人绝对路径（docs/README）" \
    '/Users/[A-Za-z0-9._-]+/|file:///Users/[A-Za-z0-9._-]+/|C:\\\\Users\\\\[A-Za-z0-9._-]+\\\\|C:/Users/[A-Za-z0-9._-]+/' \
    "README.md" \
    "README_CN.md" \
    "docs"

  print_section "5) .env.example 占位检查"
  check_env_example_api_keys

  echo
  if [[ "${EXIT_CODE}" -ne 0 ]]; then
    echo "RESULT: FAIL"
    echo "建议先执行：git status --short，并清理上面列出的命中项后再上传。"
    return "${EXIT_CODE}"
  fi

  echo "RESULT: PASS"
  if [[ "${WARNINGS}" -gt 0 ]]; then
    echo "注意：存在 ${WARNINGS} 个警告项（通常是本地文件存在但未被跟踪）。"
  fi
  echo "可安全继续执行上传前流程。"
  return 0
}

append_report_header() {
  mkdir -p "$(dirname "${REPORT_PATH}")"
  cat > "${REPORT_PATH}" <<EOF
# OpenClaw Memory Palace Release Gate Report

- Generated At: \`$(date -u +"%Y-%m-%dT%H:%M:%SZ")\`
- Project Root: \`${PROJECT_ROOT}\`
- Profile Smoke Modes: \`${PROFILE_SMOKE_MODES}\`
- Review Smoke Modes: \`${REVIEW_SMOKE_MODES}\`

EOF
}

append_report_step() {
  local order="$1"
  local title="$2"
  local status="$3"
  local workdir="$4"
  local command_text="$5"
  local duration_sec="$6"
  local log_file="$7"

  {
    printf '## %s. %s\n\n' "${order}" "${title}"
    printf -- '- Status: `%s`\n' "${status}"
    printf -- '- Workdir: `%s`\n' "${workdir}"
    printf -- '- Command: `%s`\n' "${command_text}"
    printf -- '- DurationSec: `%s`\n\n' "${duration_sec}"
    printf '```text\n'
    if [[ -f "${log_file}" ]]; then
      tail -n 200 "${log_file}"
    else
      echo "(no log captured)"
    fi
    printf '\n```\n\n'
  } >> "${REPORT_PATH}"
}

append_visual_benchmark_report_details() {
  local python_bin="$1"
  local json_path="$2"
  local markdown_path="$3"

  {
    printf '### Visual Benchmark Artifacts\n\n'
    printf -- '- JSON Artifact: `%s`\n' "${json_path}"
    printf -- '- Markdown Artifact: `%s`\n' "${markdown_path}"

    if [[ ! -f "${json_path}" ]]; then
      printf -- '- Metrics: unavailable (JSON artifact missing)\n\n'
      return
    fi
    if [[ -z "${python_bin}" ]]; then
      printf -- '- Metrics: unavailable (no Python interpreter for JSON parsing)\n\n'
      return
    fi

    printf '\n### Visual Benchmark Metrics\n\n'
    "${python_bin}" - "${json_path}" <<'PY'
import json
import sys
from pathlib import Path


def render(value):
    if value is None:
        return "-"
    return str(value)


def render_family_gate(metrics, family):
    family_summary = metrics.get("family_summary") if isinstance(metrics, dict) else None
    entry = family_summary.get(family) if isinstance(family_summary, dict) else None
    if not isinstance(entry, dict):
        return "missing"
    if entry.get("store_success_rate") == 1.0 and entry.get("search_hit_at_3_rate") == 1.0 and entry.get("get_contains_expected_rate") == 1.0:
        return "pass"
    return "fail(store={} hit={} get={})".format(
        render(entry.get("store_success_rate")),
        render(entry.get("search_hit_at_3_rate")),
        render(entry.get("get_contains_expected_rate")),
    )


payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
profiles = payload.get("profiles") if isinstance(payload, dict) else None

if isinstance(profiles, list):
    rows = [
        {
            "profile": item.get("profile", "-"),
            "status": item.get("status", "-"),
            "metrics": item.get("metrics", {}) if isinstance(item, dict) else {},
        }
        for item in profiles
    ]
    case_catalog_size = payload.get("case_catalog_size")
    executed_case_count_per_profile = payload.get("executed_case_count_per_profile")
    executed_case_count_total = payload.get("executed_case_count_total")
else:
    rows = [
        {
            "profile": payload.get("profile", "-"),
            "status": payload.get("status", "-"),
            "metrics": payload.get("metrics", {}) if isinstance(payload, dict) else {},
        }
    ]
    case_catalog_size = payload.get("case_catalog_size")
    executed_case_count_per_profile = payload.get("executed_case_count")
    executed_case_count_total = payload.get("executed_case_count")

print(f"- benchmark_status: `{render(payload.get('status'))}`")
print(f"- benchmark_partial: `{render(payload.get('partial'))}`")
print(f"- case_catalog_size: `{render(case_catalog_size)}`")
print(f"- executed_case_count_per_profile: `{render(executed_case_count_per_profile)}`")
print(f"- executed_case_count_total: `{render(executed_case_count_total)}`")
print("")
print(
    "| Profile | Status | Store | Hit@3 | MRR@3 | Get OK | Duplicate New | Visual Context | Raw Mixed | Raw Presigned | "
    "Store P95 ms | Search P95 ms | Get P95 ms | Runtime Probe | Harvest OK |"
)
print("|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---|---:|")
for row in rows:
    metrics = row["metrics"] if isinstance(row.get("metrics"), dict) else {}
    print(
        f"| {render(row.get('profile'))} | "
        f"{render(row.get('status'))} | "
        f"{render(metrics.get('store_success_rate'))} | "
        f"{render(metrics.get('search_hit_at_3_rate'))} | "
        f"{render(metrics.get('mrr_at_3'))} | "
        f"{render(metrics.get('get_contains_expected_rate'))} | "
        f"{render(metrics.get('duplicate_new_success_rate'))} | "
        f"{render(metrics.get('visual_context_reuse_success_rate'))} | "
        f"{render_family_gate(metrics, 'raw_media_mixed')} | "
        f"{render_family_gate(metrics, 'raw_media_presigned')} | "
        f"{render(metrics.get('store_p95_ms'))} | "
        f"{render(metrics.get('search_p95_ms'))} | "
        f"{render(metrics.get('get_p95_ms'))} | "
        f"{render(metrics.get('runtime_visual_probe'))} | "
        f"{render(metrics.get('runtime_visual_harvest_success_rate'))} |"
    )

coverage_gate = payload.get("coverage_gate") if isinstance(payload, dict) else None
if isinstance(coverage_gate, dict) and coverage_gate.get("required_keys"):
    print("")
    print("### Visual Benchmark Required Coverage")
    print("")
    print(f"- coverage_gate_passed: `{render(coverage_gate.get('passed'))}`")
    print(
        f"- required_coverage_keys: `{render(', '.join(coverage_gate.get('required_keys', [])))}`"
    )
    if isinstance(coverage_gate.get("profiles"), dict):
        print("")
        print("| Profile | Passed | Missing | Failing |")
        print("|---|---:|---|---|")
        for profile, entry in sorted(coverage_gate["profiles"].items()):
            missing = ", ".join(entry.get("missing_keys", [])) if isinstance(entry, dict) else ""
            failing = (
                ", ".join(sorted(entry.get("failing_keys", {}).keys()))
                if isinstance(entry, dict)
                else ""
            )
            print(
                f"| {render(profile)} | "
                f"{render(entry.get('passed') if isinstance(entry, dict) else None)} | "
                f"{render(missing or '-')} | "
                f"{render(failing or '-')} |"
            )
        print("")
        print("### Raw Media Coverage Details")
        print("")
        for row in rows:
            metrics = row["metrics"] if isinstance(row.get("metrics"), dict) else {}
            coverage_summary = (
                metrics.get("coverage_summary")
                if isinstance(metrics.get("coverage_summary"), dict)
                else {}
            )
            print(f"- {render(row.get('profile'))}:")
            for coverage_key in coverage_gate.get("required_keys", []):
                entry = coverage_summary.get(coverage_key, {})
                print(
                    f"  - {coverage_key}: cases={render(entry.get('cases'))} "
                    f"store={render(entry.get('store_success_rate'))} "
                    f"hit@3={render(entry.get('search_hit_at_3_rate'))} "
                    f"get={render(entry.get('get_contains_expected_rate'))}"
                )
    elif rows:
        metrics = rows[0]["metrics"] if isinstance(rows[0].get("metrics"), dict) else {}
        coverage_summary = (
            metrics.get("coverage_summary")
            if isinstance(metrics.get("coverage_summary"), dict)
            else {}
        )
        missing = ", ".join(coverage_gate.get("missing_keys", [])) or "-"
        failing = ", ".join(sorted(coverage_gate.get("failing_keys", {}).keys())) or "-"
        print("")
        print("| Coverage Key | Cases | Store | Hit@3 | Get OK |")
        print("|---|---:|---:|---:|---:|")
        for coverage_key in coverage_gate.get("required_keys", []):
            entry = coverage_summary.get(coverage_key, {})
            print(
                f"| {render(coverage_key)} | "
                f"{render(entry.get('cases'))} | "
                f"{render(entry.get('store_success_rate'))} | "
                f"{render(entry.get('search_hit_at_3_rate'))} | "
                f"{render(entry.get('get_contains_expected_rate'))} |"
            )
        print("")
        print(f"- missing_coverage_keys: `{render(missing)}`")
        print(f"- failing_coverage_keys: `{render(failing)}`")
PY
    printf '\n'
  } >> "${REPORT_PATH}"
}

run_release_step() {
  local order="$1"
  local title="$2"
  local workdir="$3"
  local command_text="$4"
  local timeout_seconds="${5:-${RELEASE_STEP_TIMEOUT_DEFAULT}}"

  local started_at
  started_at="$(date +%s)"
  local log_file
  log_file="$(mktemp)"
  local use_python_timeout_runner=0
  if [[ "${timeout_seconds}" =~ ^[0-9]+$ ]] && [[ "${timeout_seconds}" -gt 0 ]] && command -v python3 >/dev/null 2>&1; then
    case "${OSTYPE:-}" in
      msys*|cygwin*)
        use_python_timeout_runner=0
        ;;
      *)
        use_python_timeout_runner=1
        ;;
    esac
  fi

  printf "\n[gate %s] %s\n" "${order}" "${title}"
  set +e
  local rc
  if [[ "${use_python_timeout_runner}" -eq 1 ]]; then
    python3 - "${workdir}" "${command_text}" "${log_file}" "${timeout_seconds}" <<'PY'
import os
import signal
import subprocess
import sys
from pathlib import Path

workdir = Path(sys.argv[1])
command_text = sys.argv[2]
log_file = Path(sys.argv[3])
timeout_seconds = int(sys.argv[4])

with log_file.open("w", encoding="utf-8") as handle:
    proc = subprocess.Popen(
        ["bash", "-lc", command_text],
        cwd=str(workdir),
        stdout=handle,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
        text=True,
    )
    try:
        raise SystemExit(proc.wait(timeout=timeout_seconds))
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        with log_file.open("a", encoding="utf-8") as append_handle:
            append_handle.write(
                f"\n[release-gate-timeout] step exceeded {timeout_seconds}s and was terminated.\n"
            )
        raise SystemExit(124)
PY
    rc=$?
  else
    (
      cd "${workdir}"
      bash -lc "${command_text}"
    ) >"${log_file}" 2>&1
    rc=$?
  fi
  set -e
  local finished_at
  finished_at="$(date +%s)"
  local duration_sec=$((finished_at - started_at))
  local status="PASS"
  if [[ "${rc}" -eq 124 ]]; then
    status="TIMEOUT"
    EXIT_CODE=1
  elif [[ "${rc}" -ne 0 ]]; then
    status="FAIL"
    EXIT_CODE=1
  fi

  append_report_step "${order}" "${title}" "${status}" "${workdir}" "${command_text}" "${duration_sec}" "${log_file}"
  if [[ -f "${log_file}" ]]; then
    tail -n "${RELEASE_STEP_STDOUT_TAIL_LINES}" "${log_file}"
  fi
  rm -f "${log_file}"
}

append_release_skip() {
  local order="$1"
  local title="$2"
  local reason="$3"
  {
    printf '## %s. %s\n\n' "${order}" "${title}"
    printf -- '- Status: `SKIP`\n'
    printf -- '- Reason: %s\n\n' "${reason}"
  } >> "${REPORT_PATH}"
}

resolve_default_report_path() {
  local report_dir="${PROJECT_ROOT}/.tmp"
  mkdir -p "${report_dir}"
  local fallback_path="${report_dir}/openclaw_memory_palace_release_gate.$(date -u +"%Y%m%dT%H%M%SZ").$$.md"
  : > "${fallback_path}"
  printf '%s\n' "${fallback_path}"
}

run_release_gate() {
  local backend_python
  backend_python="$(resolve_python_from_venv "${PROJECT_ROOT}/backend/.venv" || true)"
  local repo_python
  repo_python="$(resolve_python_from_venv "${PROJECT_ROOT}/.venv" || true)"
  local profile_smoke_profiles="${PROFILE_SMOKE_PROFILES:-a,b,c,d}"
  local visual_benchmark_profiles="${VISUAL_BENCHMARK_PROFILES:-a,b}"
  local visual_benchmark_case_count="${VISUAL_BENCHMARK_CASE_COUNT:-200}"
  local visual_benchmark_case_limit="${VISUAL_BENCHMARK_CASE_LIMIT:-64}"
  local visual_benchmark_max_workers="${VISUAL_BENCHMARK_MAX_WORKERS:-1}"
  local visual_benchmark_required_coverage="${VISUAL_BENCHMARK_REQUIRED_COVERAGE:-raw_media_data_png,raw_media_data_jpeg,raw_media_data_webp,raw_media_blob,raw_media_presigned}"
  local visual_benchmark_expand_profiles_on_full_model_env="${VISUAL_BENCHMARK_EXPAND_PROFILES_ON_FULL_MODEL_ENV:-0}"
  local current_host_strict_ui_profiles="${CURRENT_HOST_STRICT_UI_PROFILES:-c,d}"
  local gate_run_token
  gate_run_token="$(date -u +"%Y%m%dT%H%M%SZ").$$"
  local release_artifact_dir="${PROJECT_ROOT}/.tmp/release-gate-artifacts"
  mkdir -p "${release_artifact_dir}"
  local onboarding_apply_validate_json_artifact="${release_artifact_dir}/openclaw_onboarding_apply_validate.${gate_run_token}.json"
  local onboarding_apply_validate_markdown_artifact="${release_artifact_dir}/openclaw_onboarding_apply_validate.${gate_run_token}.md"
  local profile_smoke_report_artifact_prefix="${release_artifact_dir}/openclaw_memory_palace_profile_smoke.${gate_run_token}"
  local compact_context_reflection_profile="${COMPACT_CONTEXT_REFLECTION_PROFILE:-c}"
  local compact_context_reflection_report_artifact="${release_artifact_dir}/openclaw_compact_context_reflection.${gate_run_token}.${compact_context_reflection_profile}.json"
  local visual_benchmark_json_artifact="${release_artifact_dir}/openclaw_visual_memory_benchmark.${gate_run_token}.json"
  local visual_benchmark_markdown_artifact="${release_artifact_dir}/openclaw_visual_memory_benchmark.${gate_run_token}.md"
  local review_smoke_report_artifact="${release_artifact_dir}/review_snapshots_http_smoke.${gate_run_token}.md"
  local current_host_strict_ui_artifact_prefix="${release_artifact_dir}/current_host_strict_ui.${gate_run_token}"

  if [[ -z "${PROFILE_SMOKE_MODEL_ENV}" && -f "${PROJECT_ROOT}/.env" ]]; then
    PROFILE_SMOKE_MODEL_ENV="${PROJECT_ROOT}/.env"
  fi
  if [[ "${visual_benchmark_expand_profiles_on_full_model_env}" == "1" ]] \
    && [[ -n "${PROFILE_SMOKE_MODEL_ENV}" ]] \
    && grep -q '^RETRIEVAL_EMBEDDING_API_KEY=' "${PROFILE_SMOKE_MODEL_ENV}" 2>/dev/null \
    && grep -q '^RETRIEVAL_EMBEDDING_MODEL=' "${PROFILE_SMOKE_MODEL_ENV}" 2>/dev/null \
    && grep -q '^RETRIEVAL_RERANKER_API_KEY=' "${PROFILE_SMOKE_MODEL_ENV}" 2>/dev/null \
    && grep -q '^RETRIEVAL_RERANKER_MODEL=' "${PROFILE_SMOKE_MODEL_ENV}" 2>/dev/null; then
    if [[ -z "${VISUAL_BENCHMARK_PROFILES:-}" ]]; then
      visual_benchmark_profiles="a,b,c,d"
    fi
  fi
  if [[ -z "${repo_python}" && -n "${backend_python}" ]]; then
    repo_python="${backend_python}"
  fi
  local quoted_backend_python=""
  local quoted_repo_python=""
  local quoted_profile_smoke_model_env=""
  local quoted_onboarding_apply_validate_json_artifact=""
  local quoted_onboarding_apply_validate_markdown_artifact=""
  local quoted_compact_context_reflection_report_artifact=""
  local quoted_visual_benchmark_json_artifact=""
  local quoted_visual_benchmark_markdown_artifact=""
  local quoted_review_smoke_report_artifact=""
  local quoted_current_host_strict_ui_artifact_prefix=""
  if [[ -n "${backend_python}" ]]; then
    quoted_backend_python="$(shell_quote "${backend_python}")"
  fi
  if [[ -n "${repo_python}" ]]; then
    quoted_repo_python="$(shell_quote "${repo_python}")"
  fi
  if [[ -n "${PROFILE_SMOKE_MODEL_ENV}" ]]; then
    quoted_profile_smoke_model_env="$(shell_quote "${PROFILE_SMOKE_MODEL_ENV}")"
  fi
  quoted_onboarding_apply_validate_json_artifact="$(shell_quote "${onboarding_apply_validate_json_artifact}")"
  quoted_onboarding_apply_validate_markdown_artifact="$(shell_quote "${onboarding_apply_validate_markdown_artifact}")"
  quoted_compact_context_reflection_report_artifact="$(shell_quote "${compact_context_reflection_report_artifact}")"
  quoted_visual_benchmark_json_artifact="$(shell_quote "${visual_benchmark_json_artifact}")"
  quoted_visual_benchmark_markdown_artifact="$(shell_quote "${visual_benchmark_markdown_artifact}")"
  quoted_review_smoke_report_artifact="$(shell_quote "${review_smoke_report_artifact}")"
  quoted_current_host_strict_ui_artifact_prefix="$(shell_quote "${current_host_strict_ui_artifact_prefix}")"
  if [[ -z "${REPORT_PATH}" ]]; then
    REPORT_PATH="$(resolve_default_report_path)"
  fi

  append_report_header

  run_release_step \
    "0" \
    "Security Scan" \
    "${PROJECT_ROOT}" \
    "bash scripts/pre_publish_check.sh"

  if [[ "${SKIP_BACKEND_TESTS}" -eq 1 ]]; then
    append_release_skip "1" "Backend Pytest" "Skipped by flag."
  elif [[ -z "${backend_python}" ]]; then
    append_release_skip "1" "Backend Pytest" "No backend python interpreter found."
    EXIT_CODE=1
  else
    run_release_step \
      "1" \
      "Backend Pytest" \
      "${PROJECT_ROOT}/backend" \
      "${quoted_backend_python} -m pytest tests -q -m 'not slow'" \
      "${RELEASE_STEP_TIMEOUT_BACKEND_PYTEST}"
  fi

  if [[ "${SKIP_BACKEND_TESTS}" -eq 1 ]]; then
    append_release_skip "1.5" "Script-Level Pytest" "Skipped with backend tests by flag."
  elif [[ -z "${repo_python}" ]]; then
    append_release_skip "1.5" "Script-Level Pytest" "No project python interpreter found."
    EXIT_CODE=1
  else
    run_release_step \
      "1.5" \
      "Script-Level Pytest" \
      "${PROJECT_ROOT}" \
      "${quoted_repo_python} -m pytest scripts/test_install_skill.py scripts/test_openclaw_memory_palace_installer.py scripts/test_openclaw_harness_cleanup_e2e.py scripts/test_openclaw_provider_retry_e2e.py scripts/test_openclaw_json_output.py scripts/test_openclaw_command_new_e2e.py scripts/test_openclaw_memory_palace_windows_native_validation.py -q" \
      "${RELEASE_STEP_TIMEOUT_SCRIPT_PYTEST}"
  fi

  if [[ "${SKIP_BACKEND_TESTS}" -eq 1 ]]; then
    append_release_skip "1.6" "Backend Benchmark Rerun Gate" "Skipped with backend tests by flag."
  elif [[ "${ENABLE_LIVE_BENCHMARK}" -ne 1 ]]; then
    append_release_skip "1.6" "Backend Benchmark Rerun Gate" "Skipped by default; pass --enable-live-benchmark to run the maintainer-only backend benchmark rerun gate."
  elif [[ -z "${backend_python}" ]]; then
    append_release_skip "1.6" "Backend Benchmark Rerun Gate" "No backend python interpreter found."
    EXIT_CODE=1
  else
    run_release_step \
      "1.6" \
      "Backend Benchmark Rerun Gate" \
      "${PROJECT_ROOT}/backend" \
      "OPENCLAW_ENABLE_LIVE_BENCHMARK=1 ${quoted_backend_python} -m pytest tests/benchmark/test_ci_regression_gate.py -q -k rerun_gate -m slow" \
      "${RELEASE_STEP_TIMEOUT_BACKEND_PYTEST}"
  fi

  if [[ "${SKIP_BACKEND_TESTS}" -eq 1 ]]; then
    append_release_skip "2" "MCP Stdio E2E" "Skipped with backend tests by flag."
  elif [[ -z "${backend_python}" ]]; then
    append_release_skip "2" "MCP Stdio E2E" "No backend python interpreter found."
    EXIT_CODE=1
  else
    run_release_step \
      "2" \
      "MCP Stdio E2E" \
      "${PROJECT_ROOT}/backend" \
      "${quoted_backend_python} -m pytest tests/test_mcp_stdio_e2e.py -q" \
      "${RELEASE_STEP_TIMEOUT_MCP_STDIO_E2E}"
  fi

  if [[ "${SKIP_PLUGIN_TESTS}" -eq 1 ]]; then
    append_release_skip "3" "Plugin Bun Tests" "Skipped by flag."
  else
    local bun_bin
    bun_bin="$(resolve_bun_bin || true)"
    if [[ -z "${bun_bin}" ]]; then
      append_release_skip "3" "Plugin Bun Tests" "bun is not installed."
      EXIT_CODE=1
    else
      local plugin_test_command
      plugin_test_command="$(cat <<EOF
set -e
cd extensions/memory-palace
${bun_bin} test \
  src/client.test.ts \
  src/smart-extraction.test.ts \
  src/assistant-derived.test.ts \
  src/host-bridge.test.ts \
  src/runtime-layout.test.ts \
  index.test.ts
if [[ ! -x ./node_modules/.bin/tsc ]]; then
  npm install --no-save typescript@^5.9.3 @types/node@^25.5.0
fi
npm exec -- tsc --project tsconfig.json --noEmit
EOF
)"
      run_release_step \
        "3" \
        "Plugin Bun Tests" \
        "${PROJECT_ROOT}" \
        "${plugin_test_command}" \
        "${RELEASE_STEP_TIMEOUT_PLUGIN_BUN_TESTS}"
    fi
  fi

  if [[ "${SKIP_PLUGIN_TESTS}" -eq 1 ]]; then
    append_release_skip "3.4" "Windows Native Validation Tests" "Skipped with plugin tests by flag."
  elif [[ "${ENABLE_WINDOWS_NATIVE_VALIDATION}" -eq 1 ]]; then
    append_release_skip "3.4" "Windows Native Validation Tests" "Legacy bash gate does not run the maintainer-only Windows native validation lane; use the Python checkpoint release gate with --enable-windows-native-validation on a real Windows host."
  else
    append_release_skip "3.4" "Windows Native Validation Tests" "Skipped by default; legacy bash gate does not run the maintainer-only Windows native validation lane. Use the Python checkpoint release gate with --enable-windows-native-validation on a real Windows host."
  fi

  if [[ "${SKIP_PLUGIN_TESTS}" -eq 1 ]]; then
    append_release_skip "3.5" "Package Install Smoke (basic+full)" "Skipped with plugin tests by flag."
  elif [[ -z "${repo_python}" ]]; then
    append_release_skip "3.5" "Package Install Smoke (basic+full)" "No project python interpreter found."
    EXIT_CODE=1
  else
    local package_install_command
    package_install_command="$(
      build_powershell_python_script_command \
        "${repo_python}" \
        "${PROJECT_ROOT}" \
        "${PROJECT_ROOT}/scripts/test_openclaw_memory_palace_package_install.py" \
        || true
    )"
    if [[ -z "${package_install_command}" ]]; then
      package_install_command="${quoted_repo_python} scripts/test_openclaw_memory_palace_package_install.py"
    fi
    run_release_step \
      "3.5" \
      "Package Install Smoke (basic+full)" \
      "${PROJECT_ROOT}" \
      "${package_install_command}" \
      "${RELEASE_STEP_TIMEOUT_PACKAGE_INSTALL}"
  fi

  if [[ "${SKIP_ONBOARDING_APPLY_VALIDATE}" -eq 1 ]]; then
    append_release_skip "3.6" "Onboarding Apply Validate E2E" "Skipped by flag."
  elif [[ "${SKIP_PLUGIN_TESTS}" -eq 1 ]]; then
    append_release_skip "3.6" "Onboarding Apply Validate E2E" "Skipped with plugin tests by flag."
  elif [[ -z "${PROFILE_SMOKE_MODEL_ENV}" ]]; then
    append_release_skip "3.6" "Onboarding Apply Validate E2E" "No profile smoke model env was provided; skipped onboarding apply/validate E2E gate."
  elif [[ -z "${repo_python}" ]]; then
    append_release_skip "3.6" "Onboarding Apply Validate E2E" "No project python interpreter found."
    EXIT_CODE=1
  elif ! command -v openclaw >/dev/null 2>&1; then
    append_release_skip "3.6" "Onboarding Apply Validate E2E" "openclaw is not installed; onboarding --apply --validate black-box E2E requires the real CLI."
    EXIT_CODE=1
  else
    local onboarding_apply_validate_command
    onboarding_apply_validate_command="$(cat <<EOF
set -e
${quoted_repo_python} scripts/test_onboarding_apply_validate_e2e.py --model-env ${quoted_profile_smoke_model_env} --report ${quoted_onboarding_apply_validate_json_artifact} --markdown ${quoted_onboarding_apply_validate_markdown_artifact} --cleanup-case-roots
EOF
)"
    run_release_step \
      "3.6" \
      "Onboarding Apply Validate E2E" \
      "${PROJECT_ROOT}" \
      "${onboarding_apply_validate_command}" \
      "${RELEASE_STEP_TIMEOUT_ONBOARDING_APPLY_VALIDATE}"
  fi

  if [[ "${SKIP_PROFILE_SMOKE}" -eq 1 ]]; then
    append_release_skip "4" "Profile Smoke" "Skipped by flag."
  elif [[ -z "${repo_python}" ]]; then
    append_release_skip "4" "Profile Smoke" "No project python interpreter found."
    EXIT_CODE=1
  else
    local profile_smoke_command
    profile_smoke_command="$(cat <<EOF
set -e
for mode in ${PROFILE_SMOKE_MODES//,/ }; do
  for profile in ${profile_smoke_profiles//,/ }; do
    report="${profile_smoke_report_artifact_prefix}.\${mode}.\${profile}.md"
    echo "[profile-smoke] mode=\${mode} profile=\${profile} report=\${report}"
    ${quoted_repo_python} scripts/openclaw_memory_palace_profile_smoke.py --modes \${mode} --profiles \${profile}${quoted_profile_smoke_model_env:+ --model-env ${quoted_profile_smoke_model_env}} --skip-frontend-e2e --report "\${report}"
  done
done
EOF
)"
    run_release_step \
      "4" \
      "Profile Smoke" \
      "${PROJECT_ROOT}" \
      "${profile_smoke_command}" \
      "${RELEASE_STEP_TIMEOUT_PROFILE_SMOKE}"
  fi

  if [[ "${SKIP_PROFILE_SMOKE}" -eq 1 ]]; then
    append_release_skip "4.5" "Compact Context Reflection E2E (${compact_context_reflection_profile})" "Skipped with profile smoke by flag."
  elif [[ "${SKIP_PHASE45}" -eq 1 ]]; then
    append_release_skip "4.5" "Compact Context Reflection E2E (${compact_context_reflection_profile})" "Skipped by flag."
  elif [[ -z "${PROFILE_SMOKE_MODEL_ENV}" ]]; then
    append_release_skip "4.5" "Compact Context Reflection E2E (${compact_context_reflection_profile})" "No profile smoke model env was provided; skipped maintainer-only compact_context reflection gate."
  elif [[ -z "${repo_python}" ]]; then
    append_release_skip "4.5" "Compact Context Reflection E2E (${compact_context_reflection_profile})" "No project python interpreter found."
    EXIT_CODE=1
  else
    run_release_step \
      "4.5" \
      "Compact Context Reflection E2E (${compact_context_reflection_profile})" \
      "${PROJECT_ROOT}" \
      "${quoted_repo_python} scripts/openclaw_compact_context_reflection_e2e.py --profile ${compact_context_reflection_profile} --model-env ${quoted_profile_smoke_model_env} --report ${quoted_compact_context_reflection_report_artifact}" \
      "${RELEASE_STEP_TIMEOUT_PROFILE_SMOKE}"
  fi

  if [[ "${SKIP_PROFILE_SMOKE}" -eq 1 ]]; then
    append_release_skip "5" "Visual Benchmark" "Skipped with profile smoke by flag."
  elif [[ -z "${repo_python}" ]]; then
    append_release_skip "5" "Visual Benchmark" "No project python interpreter found."
    EXIT_CODE=1
  else
    local visual_benchmark_timeout_seconds
    visual_benchmark_timeout_seconds="$(
      compute_visual_benchmark_timeout_seconds \
        "${visual_benchmark_profiles}" \
        "${visual_benchmark_case_limit}" \
        "${visual_benchmark_max_workers}"
    )"
    local visual_benchmark_command
    visual_benchmark_command="$(cat <<EOF
echo "[visual-benchmark-timeout] timeout=${visual_benchmark_timeout_seconds}s profiles=${visual_benchmark_profiles} case-limit=${visual_benchmark_case_limit} workers=${visual_benchmark_max_workers}"
${quoted_repo_python} scripts/openclaw_visual_memory_benchmark.py --profiles ${visual_benchmark_profiles} --case-count ${visual_benchmark_case_count} --case-limit ${visual_benchmark_case_limit} --max-workers ${visual_benchmark_max_workers}${quoted_profile_smoke_model_env:+ --model-env ${quoted_profile_smoke_model_env}} --required-coverage ${visual_benchmark_required_coverage} --json-output ${quoted_visual_benchmark_json_artifact} --markdown-output ${quoted_visual_benchmark_markdown_artifact}
EOF
)"
    run_release_step \
      "5" \
      "Visual Benchmark" \
      "${PROJECT_ROOT}" \
      "${visual_benchmark_command}" \
      "${visual_benchmark_timeout_seconds}"
    append_visual_benchmark_report_details "${repo_python}" "${visual_benchmark_json_artifact}" "${visual_benchmark_markdown_artifact}"
  fi

  if [[ "${SKIP_REVIEW_SMOKE}" -eq 1 ]]; then
    append_release_skip "6" "Review Snapshot Smoke" "Skipped by flag."
  elif [[ -z "${repo_python}" ]]; then
    append_release_skip "6" "Review Snapshot Smoke" "No project python interpreter found."
    EXIT_CODE=1
  else
    run_release_step \
      "6" \
      "Review Snapshot Smoke" \
      "${PROJECT_ROOT}" \
      "${quoted_repo_python} scripts/review_snapshots_http_smoke.py --modes ${REVIEW_SMOKE_MODES} --report ${quoted_review_smoke_report_artifact}" \
      "${RELEASE_STEP_TIMEOUT_REVIEW_SMOKE}"
  fi

  if [[ "${SKIP_FRONTEND_TESTS}" -eq 1 ]]; then
    append_release_skip "7" "Frontend Tests" "Skipped by flag."
  else
    local frontend_test_command
    frontend_test_command="$(cat <<'EOF'
set -e
npm test
npm run build
EOF
)"
    run_release_step \
      "7" \
      "Frontend Tests" \
      "${PROJECT_ROOT}/frontend" \
      "${frontend_test_command}" \
      "${RELEASE_STEP_TIMEOUT_FRONTEND_TESTS}"
  fi

  if [[ "${SKIP_FRONTEND_TESTS}" -eq 1 || "${SKIP_FRONTEND_E2E}" -eq 1 ]]; then
    append_release_skip "8" "Frontend Playwright E2E" "Skipped by flag."
  elif ! command -v openclaw >/dev/null 2>&1; then
    append_release_skip "8" "Frontend Playwright E2E" "openclaw is not installed; dashboard-auth-i18n.spec.ts requires the real CLI."
    EXIT_CODE=1
  elif ! command -v npx >/dev/null 2>&1; then
    append_release_skip "8" "Frontend Playwright E2E" "npx is not installed."
    EXIT_CODE=1
  else
    run_release_step \
      "8" \
      "Frontend Playwright E2E" \
      "${PROJECT_ROOT}/frontend" \
      "PLAYWRIGHT_E2E_API_PORT=${RELEASE_STEP_FRONTEND_E2E_API_PORT} PLAYWRIGHT_E2E_UI_PORT=${RELEASE_STEP_FRONTEND_E2E_UI_PORT} npx playwright install chromium && PLAYWRIGHT_E2E_API_PORT=${RELEASE_STEP_FRONTEND_E2E_API_PORT} PLAYWRIGHT_E2E_UI_PORT=${RELEASE_STEP_FRONTEND_E2E_UI_PORT} npm run test:e2e" \
      "${RELEASE_STEP_TIMEOUT_FRONTEND_E2E}"
  fi

  if [[ "${SKIP_CURRENT_HOST_STRICT_UI}" -eq 1 ]]; then
    append_release_skip "8.5" "Current-Host Strict UI Acceptance (C/D)" "Skipped by flag."
  elif [[ "${ENABLE_CURRENT_HOST_STRICT_UI}" -ne 1 ]]; then
    append_release_skip "8.5" "Current-Host Strict UI Acceptance (C/D)" "Disabled by default; pass --enable-current-host-strict-ui to run."
  elif ! command -v openclaw >/dev/null 2>&1; then
    append_release_skip "8.5" "Current-Host Strict UI Acceptance (C/D)" "openclaw is not installed; current-host strict UI acceptance requires the real CLI."
    EXIT_CODE=1
  elif ! command -v npx >/dev/null 2>&1; then
    append_release_skip "8.5" "Current-Host Strict UI Acceptance (C/D)" "npx is not installed."
    EXIT_CODE=1
  else
    local current_host_strict_ui_command
    current_host_strict_ui_command="$(cat <<EOF
set -e
npx playwright install chromium
for profile in ${current_host_strict_ui_profiles//,/ }; do
  screenshot_dir=${quoted_current_host_strict_ui_artifact_prefix}.\${profile}
  report="\${screenshot_dir}/webui_report.json"
  echo "[current-host-strict-ui] profile=\${profile} report=\${report}"
  OPENCLAW_ONBOARDING_USE_CURRENT_HOST=true OPENCLAW_ACCEPTANCE_STRICT_UI=true OPENCLAW_PROFILE=\${profile} OPENCLAW_SCREENSHOT_DIR="\${screenshot_dir}" OPENCLAW_REPORT_PATH="\${report}" node scripts/test_replacement_acceptance_webui.mjs
done
EOF
)"
    run_release_step \
      "8.5" \
      "Current-Host Strict UI Acceptance (C/D)" \
      "${PROJECT_ROOT}" \
      "${current_host_strict_ui_command}" \
      "${RELEASE_STEP_TIMEOUT_CURRENT_HOST_STRICT_UI}"
  fi

  {
    printf '## Summary\n\n'
    printf -- '- Result: `%s`\n' "$([[ "${EXIT_CODE}" -eq 0 ]] && echo PASS || echo FAIL)"
    printf -- '- Report Path: `%s`\n' "${REPORT_PATH}"
  } >> "${REPORT_PATH}"

  echo
  echo "RELEASE_GATE_REPORT=${REPORT_PATH}"
  if [[ "${EXIT_CODE}" -ne 0 ]]; then
    echo "RESULT: FAIL"
    return "${EXIT_CODE}"
  fi
  echo "RESULT: PASS"
  return 0
}

cd "${PROJECT_ROOT}"

if [[ "${MODE}" == "release-gate" ]]; then
  run_release_gate
else
  run_scan_mode
fi
