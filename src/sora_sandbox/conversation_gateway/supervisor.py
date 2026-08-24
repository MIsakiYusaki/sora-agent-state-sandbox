from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .client import ConversationGatewayClient, GatewayClientConfig
from .contracts import GatewayBootstrap


@dataclass
class GatewayChildProcess:
    process: subprocess.Popen[str]
    bootstrap: GatewayBootstrap

    def client(self, *, origin: str | None = None) -> ConversationGatewayClient:
        return ConversationGatewayClient(
            GatewayClientConfig(
                base_url=self.bootstrap.base_url,
                capability_token=self.bootstrap.capability_token,
                client_instance_id=self.bootstrap.client_instance_id,
                origin=origin,
            )
        )


class GatewayProcessSupervisor:
    """Desktop-owned child lifecycle with private bootstrap-token handoff."""

    def __init__(
        self,
        *,
        python_executable: str | Path = sys.executable,
        startup_timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 30.0,
    ) -> None:
        self.python_executable = str(python_executable)
        self.startup_timeout_seconds = startup_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds

    def start(
        self,
        *,
        profile_id: str,
        subject_id: str,
        agent_id: str,
        client_instance_id: str,
        origin: str | None = None,
        environment: dict[str, str] | None = None,
        model: str | None = None,
    ) -> GatewayChildProcess:
        command = [
            self.python_executable,
            "-m",
            "sora_sandbox.conversation_gateway.cli",
            "--profile-id",
            profile_id,
            "--subject-id",
            subject_id,
            "--agent-id",
            agent_id,
            "--client-instance-id",
            client_instance_id,
        ]
        if origin:
            command.extend(("--origin", origin))
        else:
            command.append("--allow-missing-origin")
        if model:
            command.extend(("--model", model))
        creation_flags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, **(environment or {})},
            creationflags=creation_flags,
        )
        try:
            bootstrap = self._read_bootstrap(process)
            self._wait_ready(process, bootstrap.base_url)
            return GatewayChildProcess(process=process, bootstrap=bootstrap)
        except Exception:
            self.stop_process(process)
            raise

    def stop(self, child: GatewayChildProcess) -> None:
        if child.process.poll() is not None:
            return
        try:
            with httpx.Client(trust_env=False, timeout=2.0) as client:
                client.post(
                    f"{child.bootstrap.base_url}/v1/admin/shutdown",
                    headers={
                        "Authorization": f"Bearer {child.bootstrap.capability_token}",
                        "X-Sora-Protocol-Version": child.bootstrap.protocol_version,
                        "X-Sora-Client-Instance": child.bootstrap.client_instance_id,
                    },
                ).raise_for_status()
            child.process.wait(timeout=self.shutdown_timeout_seconds)
        except (httpx.HTTPError, subprocess.TimeoutExpired):
            self.stop_process(child.process)

    def stop_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            process.wait(timeout=self.shutdown_timeout_seconds)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)

    def _read_bootstrap(self, process: subprocess.Popen[str]) -> GatewayBootstrap:
        if process.stdout is None:
            raise RuntimeError("gateway child stdout pipe is unavailable")
        result: queue.Queue[str] = queue.Queue(maxsize=1)

        def read_line() -> None:
            result.put(process.stdout.readline())

        threading.Thread(target=read_line, daemon=True).start()
        try:
            line = result.get(timeout=self.startup_timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError("gateway child did not produce bootstrap data") from exc
        if not line:
            raise RuntimeError(
                f"gateway child exited before bootstrap (code={process.poll()})"
            )
        return GatewayBootstrap.model_validate_json(line, strict=True)

    def _wait_ready(self, process: subprocess.Popen[str], base_url: str) -> None:
        deadline = time.monotonic() + self.startup_timeout_seconds
        last_error: Exception | None = None
        with httpx.Client(trust_env=False, timeout=1.0) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"gateway child exited during startup (code={process.returncode})"
                    )
                try:
                    response = client.get(f"{base_url}/health/ready")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError as exc:
                    last_error = exc
                time.sleep(0.05)
        raise TimeoutError(f"gateway readiness timed out: {last_error}")
