"""Shell command tool with automatic Python 3.13 virtual environment bootstrap.

Provides both:
- A native SDK ShellExecutor for use with agents.ShellTool (preferred)
- A legacy LangChain RunCommandTool for backward compatibility
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional, Sequence

from src.utils.logging import get_logger

logger = get_logger(__name__)

PYTHON_VERSION = "3.13"
DEFAULT_TIMEOUT_MS = 120_000
MAX_OUTPUT_CHARS = 12000


def _truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Trim oversized command output to keep tool responses readable."""
    normalized = (text or "").strip()
    if len(normalized) <= limit:
        return normalized
    omitted = len(normalized) - limit
    return f"{normalized[:limit]}\n\n... [truncated {omitted} chars]"


class ShellRuntimeBootstrap:
    """Prepare and reuse one agent-local Python 3.13 virtual environment for shell commands."""

    # Resolved once at class load; points to the sqlmesh-cli shim directory
    _SHIM_DIR: Optional[Path] = None

    def __init__(self, agent_path: str | Path, user_id: str = "", session_id: str = "", environment: str = "") -> None:
        self.agent_path = Path(os.path.expanduser(str(agent_path))).resolve()
        self.venv_path = self.agent_path / "venv"
        self._user_id = user_id
        self._session_id = session_id
        # Authoritative active environment selected by the user in the toolbar.
        # Injected into the sqlmesh shim so plan/apply always target the user's
        # chosen environment, regardless of what the LLM passes (anti-hallucination).
        self._environment = environment

    def ensure_ready(self) -> None:
        """Ensure the agent workspace and its Python 3.13 virtual environment are ready."""
        self.agent_path.mkdir(parents=True, exist_ok=True)
        if self._is_venv_ready():
            logger.info("Reusing shell venv: %s", self.venv_path)
            return

        python_executable = self._ensure_python313()
        self._create_virtualenv(python_executable)
        logger.info("Shell runtime initialized successfully: agent_path=%s venv=%s", self.agent_path, self.venv_path)

    async def execute_commands(
        self,
        commands: list[str],
        timeout_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Execute a batch of shell commands asynchronously inside the prepared workspace.

        Returns a list of structured result dicts with keys:
          command, stdout, stderr, exit_code, timed_out
        """
        effective_timeout = (timeout_ms or DEFAULT_TIMEOUT_MS) / 1000.0
        results: list[dict[str, Any]] = []

        for command in commands:
            normalized = (command or "").strip()
            if not normalized:
                results.append({
                    "command": command,
                    "stdout": "",
                    "stderr": "command cannot be empty",
                    "exit_code": -1,
                    "timed_out": False,
                })
                continue

            working_directory = self.agent_path
            env = self.build_command_env()
            shell_cmd = self._build_shell_command(normalized)

            logger.info("Executing shell command: cwd=%s command=%s", working_directory, normalized)
            try:
                process = await asyncio.create_subprocess_exec(
                    *shell_cmd,
                    cwd=str(working_directory),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=effective_timeout,
                )
                stdout_text = _truncate_output(stdout_bytes.decode("utf-8", errors="replace"))
                stderr_text = _truncate_output(stderr_bytes.decode("utf-8", errors="replace"))

                logger.info(
                    "Shell command finished: exit_code=%s command=%s",
                    process.returncode, normalized,
                )
                results.append({
                    "command": normalized,
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "exit_code": process.returncode or 0,
                    "timed_out": False,
                })
            except asyncio.TimeoutError:
                logger.warning("Shell command timed out after %sms: %s", timeout_ms, normalized)
                # Kill the timed-out process
                try:
                    process.kill()  # type: ignore[possibly-undefined]
                except Exception:
                    pass
                results.append({
                    "command": normalized,
                    "stdout": "",
                    "stderr": f"Command timed out after {timeout_ms}ms",
                    "exit_code": -1,
                    "timed_out": True,
                })
            except Exception as exc:
                logger.error("Shell command execution failed: %s error=%s", normalized, exc, exc_info=True)
                results.append({
                    "command": normalized,
                    "stdout": "",
                    "stderr": str(exc),
                    "exit_code": -1,
                    "timed_out": False,
                })

        return results

    def resolve_working_directory(self, path: Optional[str]) -> Path:
        """Resolve one optional working directory and keep it under the agent workspace."""
        candidate = self.agent_path
        if path:
            raw_path = Path(os.path.expanduser(path.strip()))
            candidate = raw_path if raw_path.is_absolute() else self.agent_path / raw_path

        resolved = candidate.resolve()
        if not resolved.exists():
            raise ValueError(f"working directory does not exist: {resolved}")
        if not resolved.is_dir():
            raise ValueError(f"working directory is not a directory: {resolved}")
        if not resolved.is_relative_to(self.agent_path):
            raise ValueError(f"working directory must stay inside the agent path: {resolved}")
        return resolved

    def build_command_env(self) -> dict[str, str]:
        """Build one environment that prefers the agent-local virtual environment.

        Prepend the CLI shim directory to PATH so that agent shell commands
        hit the wrappers first: the sqlmesh wrapper intercepts write
        operations and redirects them to the sqlmesh-service backend API
        (read-only commands pass through to the native binary), and the dc
        wrapper routes platform-domain commands (assets / deployment / run)
        to the dagster-service backend API.
        """
        env = os.environ.copy()
        shim_dir = self._resolve_shim_dir()
        venv_bin = str(self._venv_bin_path())
        existing_path = env.get("PATH", "")

        # PATH = {shim_dir}:{venv_bin}:{existing} — shim must come first
        env["PATH"] = f"{shim_dir}{os.pathsep}{venv_bin}{os.pathsep}{existing_path}" if existing_path else f"{shim_dir}{os.pathsep}{venv_bin}"
        env["VIRTUAL_ENV"] = str(self.venv_path)
        env.pop("PYTHONHOME", None)

        # Inject datacode platform context for the wrapper script
        env["DATACODE_WORKSPACE_PATH"] = str(self.agent_path)
        env["DATACODE_TRIGGER_TYPE"] = "agent"
        env["DATACODE_USER_ID"] = self._user_id
        env["DATACODE_SESSION_ID"] = self._session_id
        # Authoritative active environment (from toolbar). The shim treats this
        # as the single source of truth for plan/apply and overrides any
        # environment the LLM may have hallucinated in the command arguments.
        if self._environment:
            env["DATACODE_ENVIRONMENT"] = self._environment

        try:
            from src.config import settings
            env["DATACODE_SQLMESH_SERVICE_URL"] = settings.sqlmesh_service_url
        except Exception:
            # Fallback to default if settings not available (e.g. tests)
            env["DATACODE_SQLMESH_SERVICE_URL"] = "http://localhost:8002"

        # Dagster service URL for the dc platform CLI shim (assets /
        # deployment / run commands). Same fallback pattern as above.
        try:
            from src.config import settings
            env["DATACODE_DAGSTER_SERVICE_URL"] = settings.dagster_service_url
        except Exception:
            env["DATACODE_DAGSTER_SERVICE_URL"] = "http://localhost:8004"

        logger.debug("sqlmesh_shim_injected: shim_dir=%s agent_path=%s user_id=%s", shim_dir, self.agent_path, self._user_id)
        return env

    @classmethod
    def _resolve_shim_dir(cls) -> str:
        """Resolve the sqlmesh-cli shim directory relative to the repo root.

        shell.py lives at agent-service/src/tools/shell.py; the shim is at
        scripts/sqlmesh-cli/ relative to the repo root.
        """
        if cls._SHIM_DIR is not None:
            return str(cls._SHIM_DIR)

        # Walk up from this file to find the repo root
        candidate = Path(__file__).resolve().parents[3] / "scripts" / "sqlmesh-cli"
        if candidate.is_dir():
            cls._SHIM_DIR = candidate
            return str(candidate)

        # Fallback: relative to cwd (may be repo root in dev)
        fallback = Path.cwd() / "scripts" / "sqlmesh-cli"
        if fallback.is_dir():
            cls._SHIM_DIR = fallback
            return str(fallback)

        logger.warning("sqlmesh_shim_dir_not_found: tried %s and %s", candidate, fallback)
        cls._SHIM_DIR = candidate  # use expected path anyway
        return str(candidate)

    def _ensure_python313(self) -> Path:
        """Locate a Python 3.13 interpreter and return its executable path.

        When the interpreter is not found locally, behavior depends on the
        ``shell_auto_install_python`` config flag:
          - False (default): raise RuntimeError with a clear ops message.
          - True: attempt automatic installation via the system package manager.

        The auto-install path is disabled by default because it executes
        system package manager commands (including a ``curl | bash`` Homebrew
        bootstrap) at runtime — a significant RCE surface in a shared service.
        """
        detected = self._find_python313_executable()
        if detected is not None:
            logger.info("python_313_detected", interpreter=str(detected))
            return detected

        # Import here to avoid a module-level dependency on settings.
        from src.config import settings

        if not settings.shell_auto_install_python:
            raise RuntimeError(
                f"Python {PYTHON_VERSION} was not found locally and auto-install "
                f"is disabled (shell_auto_install_python=False). Install Python "
                f"{PYTHON_VERSION} manually, or set "
                f"AGENT_SERVICE_SHELL_AUTO_INSTALL_PYTHON=true / "
                f"agent.shell_auto_install_python: true in conf/agent-service.yaml "
                f"to allow automatic installation."
            )

        logger.warning(
            "python_auto_install_enabled",
            version=PYTHON_VERSION,
            hint="executing system package manager commands at runtime",
        )
        self._install_python313()

        detected = self._find_python313_executable()
        if detected is None:
            raise RuntimeError(f"Python {PYTHON_VERSION} installation finished but no usable interpreter was found")
        logger.info("python_auto_install_succeeded", interpreter=str(detected))
        return detected

    def _find_python313_executable(self) -> Optional[Path]:
        """Probe common Python launchers and well-known install paths for Python 3.13."""
        candidates: list[Sequence[str]] = []
        if platform.system() == "Windows":
            candidates.append(("py", "-3.13"))
        candidates.extend((("python3.13",), ("python3",), ("python",)))

        seen: set[str] = set()
        for candidate in candidates:
            resolved = self._probe_python_candidate(candidate)
            if resolved is None:
                continue
            resolved_key = str(resolved)
            if resolved_key in seen:
                continue
            seen.add(resolved_key)
            return resolved

        for path_candidate in self._known_python_paths():
            if not path_candidate.exists():
                continue
            resolved = self._probe_python_candidate((str(path_candidate),))
            if resolved is not None:
                return resolved
        return None

    def _probe_python_candidate(self, command: Sequence[str]) -> Optional[Path]:
        """Return the resolved executable when one launcher points to Python 3.13."""
        launcher = command[0]
        if not Path(launcher).is_absolute() and shutil.which(launcher) is None:
            return None

        try:
            completed = subprocess.run(
                [*command, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}|{sys.executable}')"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception as exc:
            logger.debug("Python launcher probe failed: launcher=%s error=%s", command, exc)
            return None

        if completed.returncode != 0:
            return None

        version, _, executable = (completed.stdout or "").strip().partition("|")
        if version != PYTHON_VERSION or not executable:
            return None
        return Path(executable).resolve()

    def _known_python_paths(self) -> list[Path]:
        """Return platform-specific fallback locations for Python 3.13."""
        system_name = platform.system()
        home = Path.home()
        if system_name == "Darwin":
            return [
                Path("/opt/homebrew/bin/python3.13"),
                Path("/usr/local/bin/python3.13"),
                Path("/opt/homebrew/opt/python@3.13/bin/python3.13"),
                Path("/usr/local/opt/python@3.13/bin/python3.13"),
            ]
        if system_name == "Windows":
            return [
                home / "AppData" / "Local" / "Programs" / "Python" / "Python313" / "python.exe",
                Path("C:/Python313/python.exe"),
            ]
        return [
            Path("/usr/bin/python3.13"),
            Path("/usr/local/bin/python3.13"),
        ]

    def _install_python313(self) -> None:
        """Install Python 3.13 using one supported system package manager."""
        system_name = platform.system()
        if system_name == "Darwin":
            brew_executable = self._detect_brew_executable()
            if brew_executable is None:
                logger.info("Homebrew was not found, attempting automatic Homebrew installation")
                self._run_checked(
                    [
                        "/bin/bash",
                        "-lc",
                        'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
                    ]
                )
                brew_executable = self._detect_brew_executable()
            if brew_executable is None:
                raise RuntimeError("Homebrew installation finished but the brew executable is still unavailable")
            self._run_checked([str(brew_executable), "install", "python@3.13"])
            return

        if system_name == "Linux":
            prefix = self._privileged_prefix()
            if shutil.which("apt-get"):
                self._run_checked([*prefix, "apt-get", "update"])
                self._run_checked([*prefix, "apt-get", "install", "-y", "python3.13", "python3.13-venv"])
                return
            if shutil.which("dnf"):
                self._run_checked([*prefix, "dnf", "install", "-y", "python3.13"])
                return
            if shutil.which("yum"):
                self._run_checked([*prefix, "yum", "install", "-y", "python3.13"])
                return
            raise RuntimeError("Unsupported Linux environment for automatic Python 3.13 installation")

        if system_name == "Windows":
            if shutil.which("winget") is None:
                raise RuntimeError("winget is required for automatic Python 3.13 installation on Windows")
            self._run_checked(
                [
                    "winget",
                    "install",
                    "-e",
                    "--id",
                    "Python.Python.3.13",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ]
            )
            return

        raise RuntimeError(f"Unsupported operating system for automatic Python 3.13 installation: {system_name}")

    def _detect_brew_executable(self) -> Optional[Path]:
        """Locate the Homebrew executable from PATH or standard macOS install paths."""
        candidates = [shutil.which("brew"), "/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path.exists():
                return path.resolve()
        return None

    def _privileged_prefix(self) -> list[str]:
        """Return an optional privilege escalation prefix for package manager commands."""
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and geteuid() == 0:
            return []
        if shutil.which("sudo"):
            return ["sudo", "-n"]
        return []

    def _create_virtualenv(self, python_executable: Path) -> None:
        """Create or repair the agent-local virtual environment with Python 3.13."""
        if self.venv_path.exists() and not self._is_venv_ready():
            logger.warning("Removing broken shell venv before recreation: %s", self.venv_path)
            shutil.rmtree(self.venv_path, ignore_errors=True)

        if self._is_venv_ready():
            return

        self._run_checked([str(python_executable), "-m", "venv", str(self.venv_path)], cwd=str(self.agent_path))
        if not self._is_venv_ready():
            raise RuntimeError(f"Virtual environment creation finished but venv is still invalid: {self.venv_path}")
        logger.info("Created shell venv: %s", self.venv_path)

    def _is_venv_ready(self) -> bool:
        """Check whether the agent-local virtual environment looks usable."""
        return self._venv_python_path().exists()

    def _venv_bin_path(self) -> Path:
        """Return the executable directory inside the virtual environment."""
        return self.venv_path / ("Scripts" if platform.system() == "Windows" else "bin")

    def _venv_python_path(self) -> Path:
        """Return the Python executable path inside the virtual environment."""
        executable_name = "python.exe" if platform.system() == "Windows" else "python"
        return self._venv_bin_path() / executable_name

    def _build_shell_command(self, command: str) -> list[str]:
        """Wrap one raw command in the platform-appropriate shell launcher."""
        if platform.system() == "Windows":
            return ["cmd", "/c", command]
        return ["/bin/bash", "-lc", command]

    def _run_checked(self, command: Sequence[str], cwd: Optional[str] = None) -> None:
        """Execute one bootstrap command and raise a detailed error on failure."""
        logger.info("Running bootstrap command: %s", " ".join(command))
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if completed.returncode == 0:
            return

        stdout = _truncate_output(completed.stdout)
        stderr = _truncate_output(completed.stderr)
        raise RuntimeError(
            "Bootstrap command failed: "
            f"{' '.join(command)}\n"
            f"Exit code: {completed.returncode}\n"
            f"Stdout: {stdout or '(empty)'}\n"
            f"Stderr: {stderr or '(empty)'}"
        )


# ══════════════════════════════════════════════════════════════
# FunctionTool factory — compatible with ChatCompletions API
# ══════════════════════════════════════════════════════════════


def create_native_shell_tool(agent_path: Optional[str] = None, user_id: str = "", session_id: str = "", environment: str = "") -> Any:
    """Create a FunctionTool-based shell tool with async batch execution.

    Returns an agents.FunctionTool instance compatible with both
    Responses API and ChatCompletions API.

    Args:
        agent_path: Workspace root directory for shell command execution.
        user_id: Current user identifier (injected as DATACODE_USER_ID).
        session_id: Current session identifier (injected as DATACODE_SESSION_ID).
        environment: Active environment selected in the toolbar (injected as
            DATACODE_ENVIRONMENT; authoritative for sqlmesh plan/apply).
    """
    try:
        from agents import FunctionTool
    except ImportError as exc:
        raise ImportError(
            "openai-agents SDK is required for shell tool. "
            f"Install: pip install openai-agents. Error: {exc}"
        ) from exc

    if not agent_path:
        raise ValueError("agent_path is required for shell tool")

    bootstrap = ShellRuntimeBootstrap(agent_path, user_id=user_id, session_id=session_id, environment=environment)
    bootstrap.ensure_ready()

    import json

    _PARAMS_SCHEMA = {
        "type": "object",
        "properties": {
            "commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of shell commands to execute sequentially inside the agent workspace. "
                    "Each command runs in the workspace root with the agent-local Python venv activated."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "description": (
                    "Maximum execution time in milliseconds for each command. "
                    f"Defaults to {DEFAULT_TIMEOUT_MS}ms if omitted."
                ),
            },
        },
        "required": ["commands"],
        "additionalProperties": False,
    }

    async def _on_invoke(_ctx: Any, raw_args: str) -> str:
        """FunctionTool invocation handler with command security policy evaluation.

        Evaluates all commands against the security policy (YAML rules):
          - deny  → all commands rejected with policy reasons, no execution
          - confirm → prompts user for batch approval; approved → execute all,
                      rejected/timeout → skip all
          - allow → execute all directly

        Each result dict includes policy_action/policy_reason fields for
        auditability.
        """
        payload = json.loads(raw_args or "{}")
        commands = payload.get("commands") or []
        timeout_ms = payload.get("timeout_ms") or DEFAULT_TIMEOUT_MS

        if not commands:
            return json.dumps({"error": "commands list cannot be empty"})

        # ------------------------------------------------------------------
        # Step 1: Security policy evaluation (batch)
        # ------------------------------------------------------------------
        from src.tools.command_policy import get_policy

        policy = get_policy()
        decision = policy.evaluate_commands(commands)

        # ------------------------------------------------------------------
        # Step 2: Dispatch based on batch decision
        # ------------------------------------------------------------------

        if decision.action == "deny":
            # Entire batch rejected — no execution
            logger.info(
                "shell_command_batch_denied",
                command_count=len(commands),
                matched_rule=decision.matched_rule,
                reason=decision.reason,
            )
            denied_results = []
            for cmd in commands:
                denied_results.append({
                    "command": cmd,
                    "stdout": "",
                    "stderr": (
                        f"Blocked by security policy [{decision.matched_rule}]: "
                        f"{decision.reason}"
                    ),
                    "exit_code": 1,
                    "timed_out": False,
                    "policy_action": "deny",
                    "policy_reason": decision.reason,
                })
            return json.dumps(denied_results, ensure_ascii=False)

        if decision.action == "confirm":
            # Batch requires human approval
            from src.tools.command_approval import request_command_approval

            approved = await request_command_approval(
                session_id=session_id,
                commands=commands,
                decision=decision,
            )

            if not approved:
                # User rejected or timed out — skip all commands
                logger.info(
                    "shell_command_batch_rejected_by_user",
                    command_count=len(commands),
                )
                skipped_results = []
                for cmd in commands:
                    skipped_results.append({
                        "command": cmd,
                        "stdout": "",
                        "stderr": (
                            f"Skipped: user rejected the command approval request "
                            f"[{decision.matched_rule}]"
                        ),
                        "exit_code": 1,
                        "timed_out": False,
                        "policy_action": "skipped",
                        "policy_reason": "User rejected batch confirmation",
                    })
                return json.dumps(skipped_results, ensure_ascii=False)

            # Approved — fall through to execute
            logger.info(
                "shell_command_batch_approved",
                command_count=len(commands),
                matched_rule=decision.matched_rule,
            )

        # ------------------------------------------------------------------
        # Step 3: Execute (allow or approved confirm)
        # ------------------------------------------------------------------
        results = await bootstrap.execute_commands(
            commands=commands,
            timeout_ms=timeout_ms,
        )

        # Annotate results with policy metadata
        for r in results:
            r["policy_action"] = decision.action
            if decision.reason:
                r["policy_reason"] = decision.reason

        return json.dumps(results, ensure_ascii=False)

    return FunctionTool(
        name="shell",
        description=(
            "Execute shell commands inside the agent workspace. Supports batch execution of "
            "multiple commands sequentially. The workspace has a Python 3.13 venv activated. "
            "Returns structured JSON with stdout, stderr, exit_code, and timed_out for each command."
        ),
        params_json_schema=_PARAMS_SCHEMA,
        on_invoke_tool=_on_invoke,
    )


__all__ = [
    "ShellRuntimeBootstrap",
    "create_native_shell_tool",
    "DEFAULT_TIMEOUT_MS",
]
