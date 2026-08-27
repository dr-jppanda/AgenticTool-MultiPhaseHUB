"""Model backends: Anthropic API, Claude Code, or Codex CLI.

Both satisfy the same contract — given a stable system prefix, a per-pass
instruction, and a Pydantic schema, return a validated instance — so
`extract.py` does not care which one is in use.

    api          anthropic SDK. Needs ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN.
    claude-code  subprocess against the installed `claude` binary. Uses whatever
                 credentials Claude Code already has (including an OAuth login),
                 so it needs no key of its own.
    codex        subprocess against `codex exec`. Reuses the local Codex login
                 and validates the final response with `--output-schema`.

Selection order (`detect_backend`):
    MHTDB_BACKEND env var -> API credentials -> `claude` -> `codex` -> error

Both backends put the paper in the *cached* position and the per-pass
instruction in the varying position, so passes 2-4 on a paper read the cache
pass 1 wrote. Measured on Claude Code 2.1.228: a 40k-token prefix costs $0.41 to
write and $0.024 to read.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_CODEX_MODEL: str | None = None  # Let the installed Codex CLI choose.


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float | None = None

    def line(self) -> str:
        c = f" cost=${self.cost_usd:.4f}" if self.cost_usd is not None else ""
        return (f"in={self.input_tokens} cache_read={self.cache_read} "
                f"cache_write={self.cache_write} out={self.output_tokens}{c}")


@dataclass
class Result:
    parsed: BaseModel
    usage: Usage = field(default_factory=Usage)


class Backend(Protocol):
    name: str
    model: str

    def complete(self, system_parts: list[str], instruction: str,
                 schema: type[T], effort: str = "high") -> Result: ...


# --------------------------------------------------------------- Anthropic API


class ApiBackend:
    name = "api"

    def __init__(self, model: str = DEFAULT_MODEL):
        import anthropic

        self.model = model
        self.client = anthropic.Anthropic()

    def complete(self, system_parts, instruction, schema, effort="high") -> Result:
        blocks = [{"type": "text", "text": p} for p in system_parts]
        # Breakpoint on the last stable block: preamble + vocabulary + paper all
        # cache together, so later passes on this paper read rather than write.
        blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}

        r = self.client.messages.parse(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            system=blocks,
            messages=[{"role": "user", "content": instruction}],
            output_format=schema,
        )
        if r.stop_reason == "refusal":
            raise RuntimeError(f"request refused: {r.stop_details}")

        u = r.usage
        return Result(
            parsed=r.parsed_output,
            usage=Usage(
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
            ),
        )


# ----------------------------------------------------------------- Claude Code


class ClaudeCodeBackend:
    """Drive a local `claude` installation in non-interactive print mode.

    The paper goes in `--system-prompt-file` (command-line arguments cannot
    carry it — a 40k-character argv entry fails outright on Windows), the
    per-pass instruction on stdin, and the Pydantic schema through
    `--json-schema`.
    """

    name = "claude-code"

    def __init__(self, model: str = DEFAULT_MODEL, binary: str | None = None,
                 timeout: int = 900, extra_args: list[str] | None = None,
                 max_turns: int = 12):
        self.binary = binary or shutil.which("claude")
        if not self.binary:
            raise RuntimeError("`claude` not found on PATH")
        self.model = model
        self.timeout = timeout
        # Delivering a large structured schema costs several turns; 3 is
        # too tight for the Conditions schema and truncates mid-call.
        self.max_turns = max_turns
        self.extra_args = extra_args or []
        # NOT resuming a session between passes, deliberately. `--resume`
        # restores the *conversation* but NOT `--system-prompt-file`, so passes
        # 2-4 would silently run without the paper and quietly return almost
        # nothing. Verified directly: after a resume the model reports the
        # system-prompt content as absent. Each pass therefore re-sends the full
        # prefix; because those bytes are identical, the server-side prompt
        # cache serves them anyway (measured: 23.7k written once, then read).
        # Older CLIs predate --tools; fall back rather than failing outright,
        # since we do not control which Claude Code version the user has.
        self._supports_tools_flag = True

    def version(self) -> str:
        try:
            out = subprocess.run([self.binary, "--version"], capture_output=True,
                                 text=True, timeout=60)
            return out.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    def complete(self, system_parts, instruction, schema, effort="high") -> Result:
        schema_json = json.dumps(schema.model_json_schema())

        cmd = [
            self.binary, "-p",
            "--output-format", "json",
            "--model", self.model,
            "--effort", effort,
            "--json-schema", schema_json,
            # Never block waiting on a permission prompt — there is no stdin
            # to answer it with.
            "--permission-mode", "dontAsk",
            "--max-turns", str(self.max_turns),
        ]

        # Disabling tools takes BOTH flags, and only one of them actually
        # disables anything:
        #   --tools ""        the real switch — "Use \"\" to disable all tools"
        #   --allowedTools "" only says which tools skip the permission prompt
        # With `--allowedTools ""` alone the model still HAS every built-in
        # tool; it just gets denied on each attempt, and every denial burns an
        # agentic turn. Measured on one extraction prompt: 4 turns / 2 denials
        # / $0.226 with allowedTools alone, versus 1 turn / 0 denials / $0.049
        # once --tools "" is passed. (Credit: the lumina project documented
        # this distinction.)
        if self._supports_tools_flag:
            cmd += ["--tools", ""]
        cmd += ["--allowedTools", ""]

        # The paper cannot ride in argv — a 40k-character argument fails
        # outright on Windows (WinError 206) — so it goes in a file, on every
        # call. See the __init__ note on why there is no --resume here.
        tmp = Path(tempfile.mkdtemp(prefix="mhtdb-"))
        sys_file = tmp / "system.txt"
        sys_file.write_text("\n\n".join(system_parts), encoding="utf-8")
        cmd += ["--system-prompt-file", str(sys_file)]

        cmd += self.extra_args
        try:
            proc = subprocess.run(cmd, input=instruction, capture_output=True,
                                  text=True, encoding="utf-8", timeout=self.timeout)
        finally:
            try:
                sys_file.unlink()
                tmp.rmdir()
            except OSError:
                pass

        if (proc.returncode != 0 and self._supports_tools_flag
                and "--tools" in (proc.stderr or "") and "nknown" in (proc.stderr or "")):
            self._supports_tools_flag = False
            log_note = "this claude build does not support --tools; falling back"
            print(f"  note: {log_note}")
            return self.complete(system_parts, instruction, schema, effort)

        if proc.returncode != 0:
            raise RuntimeError(
                f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[:500]}"
            )

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"claude returned non-JSON: {proc.stdout[:500]}")

        if envelope.get("is_error"):
            raise RuntimeError(
                f"claude reported an error (turns={envelope.get('num_turns')}, "
                f"stop={envelope.get('stop_reason')}): {str(envelope.get('result'))[:400]}"
            )

        parsed = _validate_best(envelope, schema)
        u = envelope.get("usage", {}) or {}
        return Result(
            parsed=parsed,
            usage=Usage(
                input_tokens=u.get("input_tokens", 0),
                output_tokens=u.get("output_tokens", 0),
                cache_read=u.get("cache_read_input_tokens", 0),
                cache_write=u.get("cache_creation_input_tokens", 0),
                cost_usd=envelope.get("total_cost_usd"),
            ),
        )


# ----------------------------------------------------------------------- Codex


class CodexBackend:
    """Drive a local Codex installation through non-interactive ``codex exec``.

    The full stable prefix and pass instruction travel on stdin so large papers
    never hit the Windows command-line length limit. Codex receives the
    Pydantic JSON Schema through ``--output-schema`` and runs in an empty,
    read-only temporary workspace: extraction needs no repository tools.
    """

    name = "codex"

    def __init__(self, model: str | None = DEFAULT_CODEX_MODEL,
                 binary: str | None = None, timeout: int = 900,
                 extra_args: list[str] | None = None):
        self.binary = binary or shutil.which("codex")
        if not self.binary:
            raise RuntimeError("`codex` not found on PATH")
        self.model = model or "configured-default"
        self._model_arg = model
        self.timeout = timeout
        self.extra_args = extra_args or []

    def version(self) -> str:
        try:
            out = subprocess.run([self.binary, "--version"], capture_output=True,
                                 text=True, timeout=60)
            return out.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    def complete(self, system_parts, instruction, schema, effort="high") -> Result:
        prompt = "\n\n".join(system_parts) + "\n\n# TASK\n" + instruction
        prompt += ("\n\nReturn only the JSON object required by the supplied output "
                   "schema. Do not inspect files, run commands, or use external sources.")

        with tempfile.TemporaryDirectory(prefix="mhtdb-codex-") as tmp_name:
            tmp = Path(tmp_name)
            schema_file = tmp / "schema.json"
            schema_file.write_text(
                json.dumps(_codex_output_schema(schema), ensure_ascii=False),
                encoding="utf-8",
            )
            cmd = [
                self.binary, "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox", "read-only",
                "--color", "never",
                "--output-schema", str(schema_file),
                "--config", f'model_reasoning_effort="{effort}"',
            ]
            if self._model_arg:
                cmd += ["--model", self._model_arg]
            cmd += self.extra_args
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                cwd=tmp,
            )

        if proc.returncode != 0:
            raise RuntimeError(
                f"codex exited {proc.returncode}: {_codex_failure_detail(proc)}"
            )
        if not proc.stdout.strip():
            raise RuntimeError("codex returned an empty response")

        parsed = _validate_best({"result": proc.stdout}, schema, provider="codex")
        # Plain `codex exec` leaves progress on stderr and the final structured
        # message on stdout, but exposes no stable token accounting here.
        return Result(parsed=parsed)


def _codex_output_schema(schema: type[BaseModel]) -> dict:
    """Convert Pydantic JSON Schema to Codex/OpenAI's strict subset.

    Structured Outputs requires every object to reject unknown keys and list
    every property in ``required``. Pydantic instead omits fields with defaults
    from ``required``; nullable types already preserve the intended "optional"
    value, so requiring the key does not make the value non-nullable.
    """
    raw = schema.model_json_schema()

    def strict(node):
        if isinstance(node, dict):
            # Pydantic preserves field metadata beside a reference, e.g.
            # {"$ref": "#/$defs/NumericRange", "description": "..."}.
            # OpenAI's Structured Outputs subset requires a reference node to
            # contain only $ref; `allOf` cannot be used to carry the siblings
            # because it is unsupported by the same subset.
            if "$ref" in node:
                ref = node["$ref"]
                node.clear()
                node["$ref"] = ref
                return
            node.pop("default", None)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)

    strict(raw)
    return raw


def _codex_failure_detail(proc, limit: int = 3000) -> str:
    """Keep Codex's terminal error instead of its verbose startup banner.

    ``codex exec`` writes progress, warnings, the prompt, and the final failure to
    stderr. Taking the first few hundred characters therefore hides the useful
    part (normally an API/schema/transport error) and can echo paper text. Prefer
    diagnostic lines from the tail, falling back to the raw tail only when Codex
    emitted no recognizable diagnostic.
    """
    raw = "\n".join(x for x in (proc.stderr, proc.stdout) if x).strip()
    if not raw:
        return "no diagnostic output"

    # Codex prefixes its terminal failure with ERROR:. Start at the final such
    # block so a paper sentence containing words such as "reported error bars"
    # is never mistaken for a diagnostic and echoed to the terminal.
    marker = raw.rfind("\nERROR:")
    if marker >= 0:
        return raw[marker + 1:][-limit:]
    if raw.startswith("ERROR:"):
        return raw[-limit:]

    diagnostic = [
        line for line in raw.splitlines()
        if re.match(r"^(?:\S+\s+)?(?:ERROR|WARN)\b", line)
        or re.search(r"\b(invalid schema|stream disconnected|access denied|forbidden)\b", line, re.I)
    ]
    detail = "\n".join(diagnostic[-12:]) if diagnostic else raw[-limit:]
    return detail[-limit:]


def _payload_candidates(envelope: dict) -> list:
    """Every shape the payload might arrive in, unwrapped first.

    Claude Code returns structured output in more than one shape: a flat schema
    comes back as the object itself, while a schema carrying `$defs` arrives
    wrapped as {"input": "<json string>"} because it is delivered through a
    tool call. The unwrapped form is tried first — see `_validate_best`.
    """
    out: list = []
    so = envelope.get("structured_output")
    if isinstance(so, dict) and set(so) == {"input"}:
        inner = so["input"]
        out.append(json.loads(inner) if isinstance(inner, str) else inner)
    if so is not None:
        out.append(so)
    result = envelope.get("result")
    if isinstance(result, str) and result.strip():
        out.append(_loads_loose(result))
    return [c for c in out if c is not None]


def _richness(model: BaseModel) -> int:
    """Count populated leaves — how much of the schema actually got filled."""
    def walk(v) -> int:
        if isinstance(v, BaseModel):
            return walk(v.model_dump())
        if isinstance(v, dict):
            return sum(walk(x) for x in v.values())
        if isinstance(v, (list, tuple, set)):
            return sum(walk(x) for x in v)
        if v is None or v == "" or v == "unspecified":
            return 0
        return 1

    return walk(model.model_dump())


def _validate_best(envelope: dict, schema: type[T], provider: str = "claude") -> T:
    """Validate every candidate and keep the richest one.

    Taking the *first* candidate that validates is wrong here: schemas like
    `Conditions` give every field a default, so a wrapper object such as
    {"input": "..."} validates cleanly into an all-null instance and silently
    discards the extraction. Score by populated-leaf count instead.
    """
    best: T | None = None
    best_score = -1
    errors: list[str] = []

    for c in _payload_candidates(envelope):
        try:
            m = schema.model_validate(c)
        except ValidationError as e:
            errors.append(str(e)[:200])
            continue
        score = _richness(m)
        if score > best_score:
            best, best_score = m, score

    if best is None:
        raise RuntimeError(
            f"{provider} output did not validate against {schema.__name__}. {errors[:2]}"
        )
    if best_score == 0:
        raise RuntimeError(
            f"{provider} returned an empty {schema.__name__} — every field was null. "
            "This usually means the payload shape changed; inspect the raw envelope."
        )
    return best


def _loads_loose(text: str):
    """Parse JSON that may be fenced or padded with prose."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        t = t.split("\n", 1)[1] if t.lower().startswith("json") else t
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ------------------------------------------------------------------- selection


def has_api_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def claude_code_path() -> str | None:
    return shutil.which("claude")


def codex_path() -> str | None:
    """Find Codex in PATH, an explicit override, or a VS Code installation.

    VS Code adds its extension-bundled executable to integrated-terminal PATH,
    but that entry is not guaranteed to reach a separately launched Python
    process. Looking in the extension's stable directory shape closes that gap
    without hard-coding a version number.
    """
    override = os.environ.get("MHTDB_CODEX_BINARY", "").strip().strip('"')
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return str(p.resolve())
        resolved = shutil.which(override)
        if resolved:
            return resolved

    found = shutil.which("codex")
    if found:
        return found

    home_raw = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if not home_raw:
        return None
    home = Path(home_raw)
    candidates: list[Path] = []
    for extensions in (
        home / ".vscode" / "extensions",
        home / ".vscode-insiders" / "extensions",
    ):
        if not extensions.is_dir():
            continue
        for extension in extensions.glob("openai.chatgpt-*"):
            candidates.extend(extension.glob("bin/*/codex.exe"))
    existing = [p for p in candidates if p.is_file()]
    if not existing:
        return None
    # Extension version strings are not guaranteed to sort semantically;
    # modification time reliably identifies the latest installed bundle.
    try:
        return str(max(existing, key=lambda p: p.stat().st_mtime).resolve())
    except OSError:
        return str(existing[-1].resolve())


def codex_login_status(binary: str | None = None) -> tuple[bool, str]:
    path = binary or codex_path()
    if not path:
        return False, "not installed"
    try:
        proc = subprocess.run(
            [path, "login", "status"], capture_output=True, text=True, timeout=60,
        )
        detail = (proc.stdout or proc.stderr or "unknown status").strip()
        return proc.returncode == 0, detail
    except Exception as exc:
        return False, str(exc)


def detect_backend(prefer: str | None = None, model: str | None = None) -> Backend:
    """Pick a backend. `prefer` overrides, else MHTDB_BACKEND, else auto."""
    choice = (prefer or os.environ.get("MHTDB_BACKEND") or "auto").lower()

    if choice == "api":
        if not has_api_credentials():
            raise RuntimeError("backend 'api' requested but no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN is set")
        return ApiBackend(model or DEFAULT_MODEL)
    if choice in ("claude-code", "cc", "claude_code"):
        return ClaudeCodeBackend(model or DEFAULT_MODEL)
    if choice in ("codex", "codex-cli", "codex_cli"):
        path = codex_path()
        if not path:
            raise RuntimeError(
                "backend 'codex' requested but Codex CLI was not found. Install it, "
                "add it to PATH, or set MHTDB_CODEX_BINARY to the full executable path."
            )
        logged_in, detail = codex_login_status(path)
        if not logged_in:
            raise RuntimeError(
                f"backend 'codex' requested but Codex CLI is not signed in ({detail}). "
                "Run `codex login`, then retry."
            )
        return CodexBackend(model, binary=path)
    if choice != "auto":
        raise RuntimeError(
            f"unknown backend {choice!r}; use 'api', 'claude-code', 'codex', or 'auto'"
        )

    if has_api_credentials():
        return ApiBackend(model or DEFAULT_MODEL)
    if claude_code_path():
        return ClaudeCodeBackend(model or DEFAULT_MODEL)
    cx = codex_path()
    if cx and codex_login_status(cx)[0]:
        return CodexBackend(model, binary=cx)
    raise RuntimeError(
        "No model backend available.\n"
        "  - set ANTHROPIC_API_KEY, or\n"
        "  - install Claude Code (https://claude.com/claude-code) and sign in, or\n"
        "  - install Codex and sign in (`codex login`), or\n"
        "  - run with --rules for the offline rule-based extractor."
    )


def describe_backends() -> str:
    cc = claude_code_path()
    cx = codex_path()
    lines = [
        f"  api          {'available' if has_api_credentials() else 'no credentials'}",
        f"  claude-code  {cc or 'not found on PATH'}",
        f"  codex        {cx or 'not found on PATH'}",
    ]
    if cc:
        try:
            lines[1] += f"  ({ClaudeCodeBackend().version()})"
        except Exception:
            pass
    if cx:
        try:
            logged_in, detail = codex_login_status(cx)
            auth = "signed in" if logged_in else detail
            lines[2] += f"  ({CodexBackend().version()}; {auth})"
        except Exception:
            pass
    return "\n".join(lines)
