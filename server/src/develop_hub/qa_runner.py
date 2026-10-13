from __future__ import annotations

import asyncio
import json
import re
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings


class QaRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteCommandResult:
    exit_code: int
    stdout: str
    stderr: str


class RemoteQaRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _ssh_command(self, remote_command: str) -> list[str]:
        return [
            "ssh",
            "-i", str(self.settings.qa_ssh_key),
            "-p", str(self.settings.qa_port),
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15",
            "-o", "StrictHostKeyChecking=yes",
            f"{self.settings.qa_user}@{self.settings.qa_host}",
            remote_command,
        ]

    async def _run(
        self,
        remote_command: str,
        *,
        stdin: bytes | None = None,
        timeout: int = 30,
    ) -> RemoteCommandResult:
        # Non-interactive SSH does not source the 3588 user's shell profile. Node and
        # Codex are installed together under ~/.local/bin, so make that execution
        # environment explicit for health checks and every controlled QA command.
        remote_command = 'export PATH="$HOME/.local/bin:$PATH"; ' + remote_command
        process = await asyncio.create_subprocess_exec(
            *self._ssh_command(remote_command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise QaRunnerError(f"3588 command timed out after {timeout}s") from exc
        return RemoteCommandResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def health(self) -> dict[str, Any]:
        codex = shlex.quote(self.settings.qa_codex_bin)
        ota = shlex.quote(self.settings.qa_ota_tool)
        command = (
            "set -eu; "
            "hostname; "
            f"{codex} --version; "
            "test -s \"$HOME/.codex/config.toml\" && echo agent-ready || echo agent-missing; "
            f"test -x {ota} && echo {ota} || echo ota-missing; "
            "if lsusb -d 2207:001a 2>/dev/null | grep -q .; then echo module-connected; else echo module-missing; fi"
        )
        try:
            result = await self._run(command, timeout=12)
            if result.exit_code != 0:
                raise QaRunnerError(result.stderr.strip() or "3588 health command failed")
            lines = result.stdout.splitlines()
            return {
                "online": True,
                "hostname": lines[0] if lines else None,
                "codex_version": lines[1] if len(lines) > 1 else None,
                "agent_ready": "agent-ready" in lines,
                "ota_tool": self.settings.qa_ota_tool if self.settings.qa_ota_tool in lines else None,
                "module_connected": "module-connected" in lines,
                "error": None,
            }
        except Exception as exc:
            return {
                "online": False,
                "hostname": None,
                "codex_version": None,
                "agent_ready": False,
                "ota_tool": None,
                "module_connected": False,
                "error": str(exc),
            }

    def workspace_for(self, qa_run_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f-]{36}", qa_run_id):
            raise QaRunnerError("Invalid QA run id")
        return f"{self.settings.qa_work_root}/jobs/{qa_run_id}"

    async def provision(self, qa_run_id: str, handoff: dict[str, Any]) -> str:
        workspace = self.workspace_for(qa_run_id)
        quoted = shlex.quote(workspace)
        result = await self._run(f"umask 077; mkdir -p {quoted}/evidence; cat > {quoted}/handoff.json", stdin=json.dumps(handoff, ensure_ascii=False, indent=2).encode(), timeout=20)
        if result.exit_code != 0:
            raise QaRunnerError(result.stderr.strip() or "Failed to provision 3588 QA workspace")
        return workspace

    async def run_test_case(self, workspace: str, command: str, timeout: int) -> RemoteCommandResult:
        expected_root = f"{self.settings.qa_work_root}/jobs/"
        if not workspace.startswith(expected_root):
            raise QaRunnerError("Refusing to execute outside the 3588 QA job root")
        remote = f"cd -- {shlex.quote(workspace)} && /bin/bash -lc {shlex.quote(command)}"
        return await self._run(remote, timeout=timeout)

    async def upload_firmware(
        self,
        qa_run_id: str,
        artifacts: list[dict[str, Any]],
    ) -> str:
        workspace = self.workspace_for(qa_run_id)
        remote_root = f"{workspace}/firmware"
        prepared = await self._run(f"umask 077; mkdir -p {shlex.quote(remote_root)}", timeout=20)
        if prepared.exit_code != 0:
            raise QaRunnerError(prepared.stderr.strip() or "Failed to prepare 3588 firmware directory")

        for artifact in artifacts:
            partition = str(artifact["partition"])
            digest = str(artifact["sha256"])
            local_path = Path(str(artifact["local_path"])).resolve()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", partition):
                raise QaRunnerError(f"Invalid firmware partition: {partition}")
            if not re.fullmatch(r"[0-9a-f]{64}", digest) or not local_path.is_file():
                raise QaRunnerError(f"Invalid local firmware artifact for {partition}")

            remote_path = f"{remote_root}/{partition}.img"
            staging_path = f"{remote_root}/.{partition}-{uuid.uuid4().hex}.upload"
            command = [
                "scp",
                "-i", str(self.settings.qa_ssh_key),
                "-P", str(self.settings.qa_port),
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=8",
                "-o", "ServerAliveInterval=15",
                "-o", "StrictHostKeyChecking=yes",
                "--", str(local_path),
                f"{self.settings.qa_user}@{self.settings.qa_host}:{staging_path}",
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=900)
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.communicate()
                await self._run(f"rm -f -- {shlex.quote(staging_path)}", timeout=20)
                raise QaRunnerError(f"Firmware upload timed out for {partition}") from exc
            if process.returncode != 0:
                await self._run(f"rm -f -- {shlex.quote(staging_path)}", timeout=20)
                raise QaRunnerError(stderr.decode(errors="replace")[-4000:] or stdout.decode(errors="replace")[-4000:])

            verified = await self._run(
                "set -eu; "
                f"test \"$(sha256sum {shlex.quote(staging_path)} | cut -d' ' -f1)\" = {shlex.quote(digest)}; "
                f"mv -f -- {shlex.quote(staging_path)} {shlex.quote(remote_path)}",
                timeout=120,
            )
            if verified.exit_code != 0:
                await self._run(f"rm -f -- {shlex.quote(staging_path)}", timeout=20)
                raise QaRunnerError(f"3588 SHA-256 verification failed for {partition}")
        return remote_root

    async def generate_report(
        self,
        qa_run_id: str,
        prompt: str,
        schema_path: Path,
    ) -> tuple[str, dict[str, Any], str]:
        workspace = self.workspace_for(qa_run_id)
        remote_schema = f"{workspace}/qa-report.schema.json"
        upload = await self._run(
            f"umask 077; cat > {shlex.quote(remote_schema)}",
            stdin=schema_path.read_bytes(),
            timeout=20,
        )
        if upload.exit_code != 0:
            raise QaRunnerError(upload.stderr.strip() or "Failed to upload QA report schema")
        final_path = f"{workspace}/final-report.json"
        codex_command = " ".join(
            shlex.quote(item)
            for item in [
                self.settings.qa_codex_bin,
                "exec", "--json", "--color", "never", "--sandbox", "workspace-write",
                "--cd", workspace, "--skip-git-repo-check",
                "--output-schema", remote_schema,
                "--output-last-message", final_path,
                "-",
            ]
        )
        result = await self._run(codex_command, stdin=prompt.encode(), timeout=900)
        if result.exit_code != 0:
            raise QaRunnerError(result.stderr[-4000:] or "3588 QA Agent failed")
        thread_id: str | None = None
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
        final = await self._run(f"cat -- {shlex.quote(final_path)}", timeout=20)
        if final.exit_code != 0:
            raise QaRunnerError("3588 QA Agent did not produce a final report")
        try:
            report = json.loads(final.stdout)
        except json.JSONDecodeError as exc:
            raise QaRunnerError("3588 QA Agent returned invalid report JSON") from exc
        if not thread_id:
            raise QaRunnerError("3588 QA Agent did not return a thread id")
        return thread_id, report, result.stdout

    async def install_plugin(
        self,
        bundle_path: Path,
        plugin_id: str,
        version: str,
        digest: str,
        entrypoints: list[str],
    ) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,79}", plugin_id):
            raise QaRunnerError("Invalid QA plugin id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
            raise QaRunnerError("Invalid QA plugin version")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise QaRunnerError("Invalid QA plugin digest")
        root = f"{self.settings.qa_work_root}/plugins"
        target = f"{root}/{plugin_id}/{version}/{digest[:16]}"
        staging = f"{root}/.staging-{uuid.uuid4()}"
        checks = " && ".join(
            f"test -x {shlex.quote(target + '/' + entrypoint)}" for entrypoint in entrypoints
        )
        command = (
            f"umask 077; mkdir -p {shlex.quote(root + '/' + plugin_id + '/' + version)}; "
            f"if test -d {shlex.quote(target)}; then test -f {shlex.quote(target + '/manifest.json')}; "
            f"else mkdir {shlex.quote(staging)}; "
            f"tar -xzf - -C {shlex.quote(staging)}; "
            f"test -f {shlex.quote(staging + '/manifest.json')}; "
            f"mv {shlex.quote(staging)} {shlex.quote(target)}; fi; "
            f"{checks}; printf '%s\\n' {shlex.quote(target)}"
        )
        result = await self._run(command, stdin=bundle_path.read_bytes(), timeout=300)
        if result.exit_code != 0:
            raise QaRunnerError(result.stderr.strip() or "Failed to install QA plugin on 3588")
        return target
