from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from mhtdb import backends
from mhtdb.schemas import Application, Conditions, Taxonomy, Triage, Verification


class TinyResult(BaseModel):
    answer: str


class NestedDefaults(BaseModel):
    note: str | None = None


class ResultWithDefaults(BaseModel):
    child: NestedDefaults = Field(
        default_factory=NestedDefaults, description="Pydantic puts this beside $ref"
    )
    labels: list[str] = Field(default_factory=list)


def test_codex_backend_uses_exec_stdin_and_output_schema(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs["input"]
        seen["cwd"] = kwargs["cwd"]
        schema_path = Path(cmd[cmd.index("--output-schema") + 1])
        seen["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        return SimpleNamespace(returncode=0, stdout='{"answer":"grounded"}', stderr="")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    backend = backends.CodexBackend(binary="codex", model="gpt-test")
    result = backend.complete(["SYSTEM", "PAPER"], "EXTRACT", TinyResult)

    assert result.parsed == TinyResult(answer="grounded")
    assert seen["cmd"][:2] == ["codex", "exec"]
    assert "--ephemeral" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--sandbox") + 1] == "read-only"
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "gpt-test"
    assert seen["schema"]["properties"]["answer"]["type"] == "string"
    assert seen["schema"]["additionalProperties"] is False
    assert "SYSTEM\n\nPAPER\n\n# TASK\nEXTRACT" in seen["input"]


def test_codex_schema_is_strict_recursively_and_keeps_nullable_fields():
    schema = backends._codex_output_schema(ResultWithDefaults)

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["child", "labels"]
    assert schema["properties"]["child"] == {"$ref": "#/$defs/NestedDefaults"}
    nested = schema["$defs"]["NestedDefaults"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["note"]
    assert "default" not in nested["properties"]["note"]
    assert {branch.get("type") for branch in nested["properties"]["note"]["anyOf"]} == {
        "string", "null"
    }


@pytest.mark.parametrize("model", [Triage, Conditions, Taxonomy, Application, Verification])
def test_every_extraction_schema_fits_codex_strict_subset(model):
    schema = backends._codex_output_schema(model)

    def check(node):
        if isinstance(node, dict):
            assert "default" not in node
            if "$ref" in node:
                assert set(node) == {"$ref"}
                return
            properties = node.get("properties")
            if isinstance(properties, dict):
                assert node.get("additionalProperties") is False
                assert node.get("required") == list(properties)
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


def test_codex_failure_reports_terminal_error_not_prompt_prefix():
    proc = SimpleNamespace(
        stderr=("startup\nuser\n" + "sensitive prompt " * 100
                + "\nreadings of surface superheat. Reported error bars are shown."
                + "\nERROR: Invalid schema: additionalProperties must be false\n"),
        stdout="",
    )
    detail = backends._codex_failure_detail(proc)
    assert "Invalid schema" in detail
    assert "sensitive prompt" not in detail
    assert "error bars" not in detail


def test_codex_backend_uses_cli_default_when_model_is_omitted(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert "--model" not in cmd
        return SimpleNamespace(returncode=0, stdout='{"answer":"ok"}', stderr="")

    monkeypatch.setattr(backends.subprocess, "run", fake_run)
    backend = backends.CodexBackend(binary="codex")
    assert backend.model == "configured-default"
    assert backend.complete(["paper"], "extract", TinyResult).parsed.answer == "ok"


def test_detect_backend_accepts_codex(monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    monkeypatch.setattr(backends, "codex_login_status", lambda binary=None: (True, "signed in"))
    backend = backends.detect_backend(prefer="codex", model="gpt-test")
    assert isinstance(backend, backends.CodexBackend)
    assert backend.model == "gpt-test"


def test_detect_backend_explains_missing_codex_login(monkeypatch):
    monkeypatch.setattr(backends, "codex_path", lambda: "C:/bin/codex.exe")
    monkeypatch.setattr(
        backends, "codex_login_status", lambda binary=None: (False, "Not logged in")
    )
    with pytest.raises(RuntimeError, match=r"codex login"):
        backends.detect_backend(prefer="codex")


def test_codex_path_honors_explicit_binary(monkeypatch, tmp_path):
    binary = tmp_path / "custom-codex.exe"
    binary.write_bytes(b"test")
    monkeypatch.setenv("MHTDB_CODEX_BINARY", str(binary))
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    assert backends.codex_path() == str(binary.resolve())


def test_codex_path_finds_vscode_bundled_binary(monkeypatch, tmp_path):
    binary = (
        tmp_path / ".vscode" / "extensions" / "openai.chatgpt-1.2.3-win32-x64"
        / "bin" / "windows-x86_64" / "codex.exe"
    )
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"test")
    monkeypatch.delenv("MHTDB_CODEX_BINARY", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    assert backends.codex_path() == str(binary.resolve())
