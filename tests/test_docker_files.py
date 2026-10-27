"""Static checks of the Docker files: the compose services, .dockerignore, Dockerfile and the Docker CI workflow.

These need no Docker daemon and no network. They guard the intent of the files (the demo service is isolated, secrets and
local databases stay out of the image, the CI job pins its actions and publishes nothing); whether the image really
builds and runs is only known from the Docker workflow in CI.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
WORKFLOW_TEXT = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)
SERVICES = COMPOSE["services"]
PIPELINE = ("init", "scheduler", "dashboard")


# ---- compose ----------------------------------------------------------------

def test_demo_service_is_isolated_and_keyless():
    """dashboard-demo is in the demo profile, needs no .env, volume or init, runs DEMO_MODE and binds to loopback."""
    demo = SERVICES["dashboard-demo"]
    assert demo["profiles"] == ["demo"]
    assert "env_file" not in demo and "volumes" not in demo and "depends_on" not in demo
    assert demo["environment"]["DEMO_MODE"] == "true"
    assert demo["command"][:3] == ["streamlit", "run", "app.py"]
    assert demo["ports"] == ["127.0.0.1:8502:8501"]
    assert demo["image"] == SERVICES["dashboard"]["image"]


def test_plain_up_still_starts_the_same_services_as_before():
    """The three pipeline services have no profile (so a plain `up` starts them), only the demo service has one."""
    assert [name for name, svc in SERVICES.items() if "profiles" not in svc] == list(PIPELINE)
    assert [name for name, svc in SERVICES.items() if "profiles" in svc] == ["dashboard-demo"]


@pytest.mark.parametrize("name", PIPELINE)
def test_env_file_is_optional_for_the_pipeline_services(name):
    """.env may be missing (so the demo profile works on a machine without one), and the data volume is still mounted."""
    assert SERVICES[name]["env_file"] == [{"path": ".env", "required": False}]
    assert SERVICES[name]["volumes"] == ["oilgas_data:/data"]


def test_pipeline_services_are_unchanged():
    """Commands, ordering, health checks and the loopback-only port of the original services are as before."""
    assert SERVICES["init"]["command"] == ["python", "-c", "import storage"]
    assert SERVICES["scheduler"]["command"] == ["python", "-m", "jobs.scheduler"]
    assert SERVICES["dashboard"]["ports"] == ["127.0.0.1:8501:8501"]
    for name in ("scheduler", "dashboard"):
        assert SERVICES[name]["depends_on"] == {"init": {"condition": "service_completed_successfully"}}
        assert SERVICES[name]["healthcheck"]["test"][:3] == ["CMD", "python", "healthcheck.py"]


def test_no_service_publishes_a_port_beyond_loopback():
    """Every published port is bound to 127.0.0.1 (the dashboard has no login by default)."""
    for svc in SERVICES.values():
        assert all(str(p).startswith("127.0.0.1:") for p in svc.get("ports", []))


def test_header_documents_the_safe_demo_command():
    """The compose file tells people to name the service, and warns against a bare --profile demo up."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "docker compose up --build dashboard-demo" in text and "Do NOT run `docker compose --profile demo up`" in text


# ---- .dockerignore ------------------------------------------------------------

def _rule_regex(pattern):
    """Translate a .dockerignore pattern (Go filepath.Match plus **) into a regex for a root-relative path."""
    pattern = pattern.strip("/")
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}(?:/.*)?$")  # a pattern also covers everything below a matching directory


def excluded(path):
    """Whether the build context drops `path`: the last matching line wins and a leading ! re-includes."""
    result = False
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        if _rule_regex(line.lstrip("!")).match(path):
            result = not negate
    return result


@pytest.mark.parametrize("path", [".env", "dashboard_store.db", "dashboard_store.backup.db", "data/deep/store.db",
                                  "tests/fixtures/x.db", "demo/other.db", "demo/demo_store.db-wal", "oil_gas_archive/a.json",
                                  "eval_history.jsonl", "venv/Scripts/python.exe", ".git/config", ".github/workflows/ci.yml",
                                  "docs/REFERENCE.md", "tests/test_core_units.py", "outbox/a.eml", "reports/symbol_check.json"])
def test_secrets_local_data_and_dev_files_are_excluded(path):
    """Secrets, every database at any depth, local data and files not needed at runtime stay out of the image."""
    assert excluded(path)


@pytest.mark.parametrize("path", ["demo/demo_store.db", "demo/snapshot_meta.json", "app.py", "config.py", "demo_mode.py",
                                  "healthcheck.py", "jobs/scheduler.py", "pages/1_Research.py", "config/market_symbols.json",
                                  ".streamlit/config.toml", "requirements.txt", "constraints.txt", "scripts/make_demo_snapshot.py"])
def test_runtime_files_and_the_demo_snapshot_are_included(path):
    """Everything the containers need, including the demo snapshot (the only database), is in the build context."""
    assert not excluded(path)


# ---- Dockerfile ---------------------------------------------------------------

def test_dockerfile_installs_pinned_dependencies_before_copying_code_and_runs_non_root():
    """Dependencies install from constraints.txt before the code is copied, and the process runs as a non-root user."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert text.index("pip install -r requirements.txt -c constraints.txt") < text.index("COPY . .")
    assert re.search(r"^FROM python:3\.11", text, re.M)
    assert re.search(r"^USER app$", text, re.M) and text.index("useradd") < text.index("USER app")


# ---- the Docker workflow ---------------------------------------------------------

def test_workflow_triggers_and_permissions():
    """It runs on push and pull request with read-only repository permissions."""
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))  # PyYAML reads the bare key `on` as True
    assert set(triggers) == {"push", "pull_request"}
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_workflow_uses_no_secrets_and_publishes_nothing():
    """No secrets are referenced, nothing logs in to a registry, and the image build does not push."""
    assert "secrets." not in WORKFLOW_TEXT and "docker/login-action" not in WORKFLOW_TEXT
    build = [s for job in WORKFLOW["jobs"].values() for s in job["steps"]
             if str(s.get("uses", "")).startswith("docker/build-push-action")]
    assert len(build) == 1 and build[0]["with"]["push"] is False and build[0]["with"]["load"] is True


def test_workflow_actions_are_pinned_to_versions():
    """Every action is pinned to a version tag, never a branch."""
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW_TEXT)
    assert uses and all(re.search(r"@v\d+(\.\d+){0,2}$", u) for u in uses), uses
    assert "hadolint/hadolint-action@v3.5.0" in uses


def test_workflow_covers_lint_compose_build_and_smoke_tests():
    """The jobs and the key smoke checks from the task are all present."""
    assert set(WORKFLOW["jobs"]) == {"hadolint", "compose", "build-and-smoke"}
    for needle in ("docker compose config -q", "docker compose --profile demo config -q", "healthcheck.py dashboard",
                   "import config, storage, jobs.scheduler", "DEMO_MODE=true", "/app/.env", "demo_store.db", "AppTest"):
        assert needle in WORKFLOW_TEXT, needle
