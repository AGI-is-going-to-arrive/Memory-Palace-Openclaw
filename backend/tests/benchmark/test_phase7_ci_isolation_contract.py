import os
from pathlib import Path
import re
import subprocess
import tempfile

import pytest


BENCHMARK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_DIR.parents[2]
REPO_ROOT = PROJECT_ROOT

WORKFLOW_PATH = REPO_ROOT / ".github/workflows/benchmark-gate.yml"
DOCKERIGNORE_PATH = PROJECT_ROOT / ".dockerignore"
BACKEND_DOCKERFILE_PATH = PROJECT_ROOT / "deploy/docker/Dockerfile.backend"
DOCKER_ONE_CLICK_SH_PATH = PROJECT_ROOT / "scripts/docker_one_click.sh"
DOCKER_ONE_CLICK_PS1_PATH = PROJECT_ROOT / "scripts/docker_one_click.ps1"
RUN_POST_CHANGE_CHECKS_PATH = REPO_ROOT / "new/run_post_change_checks.sh"
RUN_PWSH_DOCKER_REAL_TEST_PATH = REPO_ROOT / "new/run_pwsh_docker_real_test.sh"
PRE_PUBLISH_CHECK_PATH = PROJECT_ROOT / "scripts/pre_publish_check.sh"
README_PATH = PROJECT_ROOT / "README.md"


def _load_nonempty_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        lines.append(text)
    return lines


def test_phase7_benchmark_workflow_is_retired_and_documented() -> None:
    assert not WORKFLOW_PATH.exists(), "benchmark workflow gate file should remain retired in the current repo"
    readme_text = README_PATH.read_text(encoding="utf-8")

    assert "repo no longer keeps active `.github/workflows/*` files" in readme_text
    assert "local/package-backed" in readme_text


def test_phase7_dockerignore_excludes_test_and_doc_assets_from_images() -> None:
    assert DOCKERIGNORE_PATH.exists(), "missing project .dockerignore"
    lines = _load_nonempty_lines(DOCKERIGNORE_PATH)

    assert "backend/tests/" in lines
    assert "docs/" in lines
    assert "snapshots/" in lines


def test_phase7_backend_dockerfile_relies_on_backend_copy_with_dockerignore_guard() -> None:
    assert BACKEND_DOCKERFILE_PATH.exists(), "missing backend Dockerfile"
    text = BACKEND_DOCKERFILE_PATH.read_text(encoding="utf-8")

    assert "COPY backend /app/backend" in text
    assert "COPY . /app" not in text
    assert "backend/tests" not in text


def test_phase7_scripts_reserve_ports_before_parallel_compose_up() -> None:
    shell_text = DOCKER_ONE_CLICK_SH_PATH.read_text(encoding="utf-8")
    ps1_text = DOCKER_ONE_CLICK_PS1_PATH.read_text(encoding="utf-8")
    post_check_text = RUN_POST_CHANGE_CHECKS_PATH.read_text(encoding="utf-8")

    assert "memory-palace-port-locks" in shell_text
    assert "memory-palace-port-locks" in ps1_text
    assert "memory-palace-port-locks" in post_check_text
    assert "try_acquire_path_lock" in shell_text
    assert "Try-AcquirePathLock" in ps1_text
    assert "reserve_exact_port_if_available" in post_check_text


def test_phase7_scripts_use_isolated_env_files_and_checkout_deploy_lock() -> None:
    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    shell_text = DOCKER_ONE_CLICK_SH_PATH.read_text(encoding="utf-8")
    ps1_text = DOCKER_ONE_CLICK_PS1_PATH.read_text(encoding="utf-8")
    post_check_text = RUN_POST_CHANGE_CHECKS_PATH.read_text(encoding="utf-8")

    assert "MEMORY_PALACE_DOCKER_ENV_FILE" in compose_text
    assert "memory-palace-docker-env-" in shell_text
    assert "memory-palace-docker-env-" in ps1_text
    assert "memory-palace-deploy-locks" in shell_text
    assert "memory-palace-deploy-locks" in ps1_text
    assert "DEPLOYMENT_LOCK" in shell_text
    assert "$script:DeploymentLockDir" in ps1_text
    assert "another docker_one_click deployment is already running for this checkout" in shell_text
    assert "another docker_one_click deployment is already running for this checkout" in ps1_text
    assert "memory-palace-post-change-checks" in post_check_text
    assert "Another run_post_change_checks.sh process is already active for this workspace." in post_check_text


def test_phase7_docker_compose_persists_snapshots_and_entrypoint_prepares_mount() -> None:
    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    shell_text = DOCKER_ONE_CLICK_SH_PATH.read_text(encoding="utf-8")
    ps1_text = DOCKER_ONE_CLICK_PS1_PATH.read_text(encoding="utf-8")
    backend_entrypoint_text = (PROJECT_ROOT / "deploy/docker/backend-entrypoint.sh").read_text(
        encoding="utf-8"
    )

    assert "- memory_palace_snapshots:/app/snapshots" in compose_text
    assert "MEMORY_PALACE_SNAPSHOTS_VOLUME" in compose_text
    assert "NOCTURNE_SNAPSHOTS_VOLUME" in compose_text
    assert "resolve_snapshots_volume" in shell_text
    assert "MEMORY_PALACE_SNAPSHOTS_VOLUME" in shell_text
    assert "Resolve-SnapshotsVolume" in ps1_text
    assert "MEMORY_PALACE_SNAPSHOTS_VOLUME" in ps1_text
    assert "mkdir -p /app/data /app/snapshots" in backend_entrypoint_text
    assert "chown -R app:app /app/data /app/snapshots" in backend_entrypoint_text


def test_phase7_docker_compose_embeds_sse_in_backend_before_frontend_start() -> None:
    compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "condition: service_healthy" in compose_text
    assert "\n  sse:\n" not in compose_text
    assert 'urllib.request.urlopen(\'http://127.0.0.1:8000/health\'' in compose_text


def test_phase7_nginx_sse_locations_clear_connection_header() -> None:
    nginx_text = (PROJECT_ROOT / "deploy/docker/nginx.conf.template").read_text(
        encoding="utf-8"
    )

    assert "proxy_pass http://backend:8000;" in nginx_text
    assert "proxy_pass http://sse:8000;" not in nginx_text
    assert nginx_text.count('proxy_set_header Connection "";') >= 3
    assert "location ^~ /sse/" in nginx_text


def test_phase7_post_change_checks_include_review_snapshots_http_smoke_gate() -> None:
    post_check_text = RUN_POST_CHANGE_CHECKS_PATH.read_text(encoding="utf-8")

    assert "run_review_snapshots_http_smoke_gate()" in post_check_text
    assert "workspace.review_snapshots_http_smoke" in post_check_text
    assert "review_snapshots_http_smoke.py" in post_check_text
    assert 'local-only mode to avoid duplicating compose lifecycle' in post_check_text
    assert '--modes "${smoke_modes}" --report "${report_file}"' in post_check_text


def test_phase7_windows_equivalent_pwsh_gate_preserves_skip_status() -> None:
    post_check_text = RUN_POST_CHANGE_CHECKS_PATH.read_text(encoding="utf-8")
    pwsh_text = RUN_PWSH_DOCKER_REAL_TEST_PATH.read_text(encoding="utf-8")
    gate_start = post_check_text.index("run_windows_equivalent_pwsh_docker_gate() {")
    gate_end = post_check_text.index("append_review_record()", gate_start)
    gate_text = post_check_text[gate_start:gate_end]

    assert 'pwsh_exit_code' in gate_text
    assert 'status="SKIP"' in gate_text
    assert 'if [[ "${status}" == "FAIL" ]]; then' in gate_text
    assert "docker_run_exit" in pwsh_text


def test_phase7_post_check_exit_trap_is_root_guarded_and_pwsh_temp_json_is_cleaned() -> None:
    post_check_text = RUN_POST_CHANGE_CHECKS_PATH.read_text(encoding="utf-8")
    pwsh_text = RUN_PWSH_DOCKER_REAL_TEST_PATH.read_text(encoding="utf-8")

    assert 'ROOT_BASHPID="${BASHPID:-$$}"' in post_check_text
    assert 'if [[ "${BASHPID:-$$}" != "${ROOT_BASHPID}" ]]; then' in post_check_text
    assert 'if (cd "${REPO_ROOT}" && bash new/run_pwsh_docker_real_test.sh --env-file "${RUNTIME_ENV_FILE}" --output-json "${result_json}"); then' in post_check_text
    assert 'if ! (cd "${REPO_ROOT}" && bash new/run_pwsh_docker_real_test.sh --env-file "${RUNTIME_ENV_FILE}" --output-json "${result_json}"); then' not in post_check_text
    assert 'HOST_RESULT_JSON="${SCRIPT_DIR}/.tmp-pwsh_docker_real_test_result_${RUN_TOKEN}.json"' in pwsh_text
    assert 'CONTAINER_RESULT_JSON="/work/new/.tmp-pwsh_docker_real_test_result_${RUN_TOKEN}.json"' in pwsh_text
    assert '.tmp-pwsh_docker_real_test_result_' in pwsh_text
    assert 'if [[ -f "${HOST_RESULT_JSON}" && "${OUTPUT_JSON}" != "${HOST_RESULT_JSON}" ]]; then' in pwsh_text
    assert 'cp "${HOST_RESULT_JSON}" "${OUTPUT_JSON}" 2>/dev/null || true' in pwsh_text
    assert 'rm -f "${HOST_RESULT_JSON}" 2>/dev/null || true' in pwsh_text


def test_phase7_release_gate_default_report_path_is_unique_per_run() -> None:
    if os.name == "nt":
        pytest.skip("bash release-gate execution contract is covered on POSIX hosts; Windows uses the pwsh-equivalent path.")

    raw_script = PRE_PUBLISH_CHECK_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    temp_dir = PROJECT_ROOT / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".sh",
        delete=False,
        encoding="utf-8",
        newline="\n",
        dir=temp_dir,
    ) as handle:
        handle.write(raw_script)
        script_path = Path(handle.name)
    command = [
        "bash",
        str(script_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "--release-gate",
        "--skip-backend-tests",
        "--skip-plugin-tests",
        "--skip-profile-smoke",
        "--skip-review-smoke",
        "--skip-frontend-tests",
    ]
    env = os.environ.copy()
    try:
        procs = [
            subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]

        outputs: list[tuple[str, str]] = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=120)
            assert proc.returncode == 0, stderr or stdout
            outputs.append((stdout, stderr))
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass

    report_paths: list[Path] = []
    for stdout, _stderr in outputs:
        match = re.search(r"RELEASE_GATE_REPORT=(.+)", stdout)
        assert match, stdout
        report_paths.append(Path(match.group(1).strip()))

    assert report_paths[0] != report_paths[1]
    for report_path in report_paths:
        assert report_path.is_file(), report_path
        text = report_path.read_text(encoding="utf-8")
        assert text.count("## 0. Security Scan") == 1
        assert text.count("## Summary") == 1


def test_phase7_release_gate_uses_run_scoped_visual_artifacts_and_inlines_metrics() -> None:
    text = PRE_PUBLISH_CHECK_PATH.read_text(encoding="utf-8")

    assert 'local gate_run_token' in text
    assert 'local release_artifact_dir="${PROJECT_ROOT}/.tmp/release-gate-artifacts"' in text
    assert 'profile_smoke_profiles="${PROFILE_SMOKE_PROFILES:-a,b,c,d}"' in text
    assert 'openclaw_memory_palace_profile_smoke.${gate_run_token}' in text
    assert '--modes \\${mode} --profiles \\${profile}' in text
    assert '--skip-frontend-e2e --report "\\${report}"' in text
    assert 'openclaw_visual_memory_benchmark.${gate_run_token}.json' in text
    assert 'openclaw_visual_memory_benchmark.${gate_run_token}.md' in text
    assert 'append_visual_benchmark_report_details "${repo_python}" "${visual_benchmark_json_artifact}" "${visual_benchmark_markdown_artifact}"' in text
    assert '### Visual Benchmark Artifacts' in text
    assert '### Visual Benchmark Metrics' in text
    assert '### Visual Benchmark Required Coverage' in text
    assert '### Raw Media Coverage Details' in text
    assert 'VISUAL_BENCHMARK_EXPAND_PROFILES_ON_FULL_MODEL_ENV' in text
    assert 'visual_benchmark_profiles="${VISUAL_BENCHMARK_PROFILES:-a,b}"' in text
    assert 'visual_benchmark_required_coverage="${VISUAL_BENCHMARK_REQUIRED_COVERAGE:-raw_media_data_png,raw_media_data_jpeg,raw_media_data_webp,raw_media_blob,raw_media_presigned}"' in text
    assert 'visual_benchmark_max_workers="${VISUAL_BENCHMARK_MAX_WORKERS:-1}"' in text
    assert '--required-coverage ${visual_benchmark_required_coverage}' in text
    assert 'dashboard-auth-i18n.spec.ts requires the real CLI.' in text
    assert 'RELEASE_STEP_TIMEOUT_BACKEND_PYTEST' in text
    assert 'status="TIMEOUT"' in text
    assert "[release-gate-timeout] step exceeded" in text


def test_phase7_release_gate_separates_default_backend_pytest_from_live_benchmark_lane() -> None:
    text = PRE_PUBLISH_CHECK_PATH.read_text(encoding="utf-8")

    assert '"Backend Pytest"' in text
    assert 'pytest tests -q -m \'not slow\'' in text
    assert '"Backend Benchmark Rerun Gate"' in text
    assert 'test_ci_regression_gate.py -q -k rerun_gate -m slow' in text
    assert 'Skipped by default; pass --enable-live-benchmark to run the maintainer-only backend benchmark rerun gate.' in text


def test_phase7_release_gate_skips_windows_native_validation_by_default() -> None:
    text = PRE_PUBLISH_CHECK_PATH.read_text(encoding="utf-8")

    assert '--enable-windows-native-validation' in text
    assert '"Windows Native Validation Tests"' in text
    assert 'Skipped by default; legacy bash gate does not run the maintainer-only Windows native validation lane.' in text
    assert 'Legacy bash gate does not run the maintainer-only Windows native validation lane; use the Python checkpoint release gate with --enable-windows-native-validation on a real Windows host.' in text
    assert 'scripts/test_openclaw_memory_palace_windows_native_validation.py -q' in text


def test_pre_publish_check_scans_docs_for_cross_user_absolute_paths() -> None:
    text = PRE_PUBLISH_CHECK_PATH.read_text(encoding="utf-8")

    assert 'print_section "4b) 通用个人路径泄露扫描（docs/README，仅扫描已跟踪文件）"' in text
    assert '"README.md"' in text
    assert '"README_CN.md"' in text
    assert '"docs"' in text
    assert '/Users/[A-Za-z0-9._-]+/' in text
    assert 'C:\\\\\\\\Users\\\\\\\\[A-Za-z0-9._-]+\\\\\\\\' in text
