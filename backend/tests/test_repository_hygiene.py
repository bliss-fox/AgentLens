import importlib.util
import tomllib
from pathlib import Path


def load_hygiene_verifier():
    path = Path(__file__).parents[1] / "scripts" / "verify_repository_hygiene.py"
    spec = importlib.util.spec_from_file_location("repository_hygiene", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_hygiene_scanner_reports_location_without_secret_value():
    verifier = load_hygiene_verifier()
    token = "sk-" + "a" * 24
    absolute_path = "C:" + "\\Users\\developer\\project"

    findings = verifier.scan_text(
        "config.txt",
        f"TOKEN={token}\nWORKSPACE={absolute_path}\n",
    )
    rendered = "\n".join(finding.render() for finding in findings)

    assert {finding.category for finding in findings} == {"api_token", "absolute_path"}
    assert token not in rendered
    assert absolute_path not in rendered
    assert "config.txt:1" in rendered
    assert "config.txt:2" in rendered


def test_hygiene_scanner_allows_documented_placeholders():
    verifier = load_hygiene_verifier()

    findings = verifier.scan_text(
        ".env.example",
        "OPENAI_API_KEY=\nENDPOINT=http://127.0.0.1:8000\n",
    )

    assert findings == []
    assert verifier.is_secret_env_file(".env")
    assert verifier.is_secret_env_file("frontend/.env.local")
    assert not verifier.is_secret_env_file(".env.example")


def test_direct_dependencies_have_lower_and_upper_bounds():
    root = Path(__file__).parents[2]
    pyproject = tomllib.loads((root / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    dependency_groups = [
        pyproject["project"]["dependencies"],
        pyproject["project"]["optional-dependencies"]["dev"],
        pyproject["build-system"]["requires"],
    ]

    for dependency in (item for group in dependency_groups for item in group):
        assert ">=" in dependency, f"direct dependency has no lower bound: {dependency}"
        assert "<" in dependency, f"direct dependency has no upper bound: {dependency}"


def test_repository_hygiene_is_a_ci_gate_and_local_artifacts_are_ignored():
    root = Path(__file__).parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ignore = (root / ".gitignore").read_text(encoding="utf-8")

    assert workflow.index("name: Repository hygiene") < workflow.index("name: Ruff")
    rules = (
        ".env.*",
        "!.env.example",
        ".pytest-tmp-*/",
        "/evidence/compose-verification*.json",
    )
    for rule in rules:
        assert rule in ignore
