"""Verify frozen Gateway startup, authentication and shutdown without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def verify(binary: Path, temporary: str) -> dict:
    env = dict(os.environ)
    env.update(
        SORA_GATEWAY_STATE_ROOT=temporary,
        SORA_SANDBOX_STATE_DIR=temporary,
        SORA_SANDBOX_CONFIG_DIR=temporary,
    )
    process = subprocess.Popen(
        [str(binary), "--profile-id", "package-smoke", "--subject-id", "package-smoke-user",
         "--agent-id", "sora", "--client-instance-id", "package-smoke-client",
         "--origin", "tauri://localhost", "--log-level", "error", "--no-legacy-migration"],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    lines = queue.Queue()

    def read_bootstrap():
        lines.put(process.stdout.readline(16385))

    reader = threading.Thread(target=read_bootstrap, daemon=True)
    reader.start()
    report = {"scope": "packaged-sidecar-isolated-no-model-calls"}
    request = None
    native_fixture = None
    try:
        line = lines.get(timeout=45)
        bootstrap = json.loads(line)
        if bootstrap.get("schema_version") != "ConversationGatewayBootstrap.v1":
            raise RuntimeError("invalid_bootstrap")
        base = bootstrap["base_url"]
        url = urllib.parse.urlparse(base)
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
            raise RuntimeError("invalid_gateway_address")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        headers = {
            "Authorization": "Bearer " + bootstrap["capability_token"],
            "Origin": "tauri://localhost",
            "X-Sora-Client-Instance": "package-smoke-client",
            "X-Sora-Protocol-Version": "1.0",
        }

        def request(path, *, authenticated=True, method="GET"):
            sent = headers if authenticated else {
                key: value for key, value in headers.items() if key != "Authorization"
            }
            req = urllib.request.Request(base + path, headers=sent, method=method)
            try:
                with opener.open(req, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                error.close()
                return error.code, {}

        for _ in range(40):
            try:
                code, _ = request("/health/live", authenticated=False)
                if code == 200:
                    break
            except urllib.error.URLError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("health_startup_failed")
        report["health_status"] = code
        path = "/v1/debug/snapshot?conversation_id=package-smoke-conversation"
        denied, _ = request(path, authenticated=False)
        report["unauthenticated_debug_status"] = denied
        if denied not in (401, 403):
            raise RuntimeError("authentication_not_enforced")
        code, snapshot = request(path)
        if (code != 200 or snapshot.get("schema_version") != "DesktopDebugSnapshot.v1"
                or snapshot.get("timeline", {}).get("turns") != []):
            raise RuntimeError("invalid_debug_snapshot")
        report.update(debug_status=code, empty_turns=True)
        code, _ = request("/v1/admin/shutdown", method="POST")
        if code != 202:
            raise RuntimeError("shutdown_rejected")
        report["shutdown_status"] = code
        report["process_exit"] = process.wait(timeout=20)
        if report["process_exit"] != 0:
            raise RuntimeError("shutdown_failed")
        identity = hashlib.sha256(b"package-smoke\x1fpackage-smoke-user\x1fsora").hexdigest()
        directory = Path(temporary) / "gateway-identities" / identity
        # Reproduce compatibility state files longer than Windows MAX_PATH.
        fixture = directory / ("compatibility-state-" * 5) / ("profile-" * 15 + ".json")
        native_fixture = Path("\\\\?\\" + str(fixture)) if os.name == "nt" else fixture
        native_fixture.parent.mkdir(parents=True)
        native_fixture.write_text("isolated-packaging-fixture", encoding="utf-8")
        confirmation = {"confirmed": True, "profile_id": "package-smoke", "confirmation": "erase:package-smoke"}
        erased = subprocess.run(
            [str(binary), "--erase-stopped-profile", "--profile-id", "package-smoke",
             "--subject-id", "package-smoke-user", "--agent-id", "sora"],
            input=json.dumps(confirmation) + "\n", env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        receipt = json.loads(erased.stdout)
        if erased.returncode != 0 or receipt.get("status") != "erased" or directory.exists():
            raise RuntimeError("packaged_erasure_failed")
        report["extended_length_erasure"] = "passed"
        fenced = subprocess.run(
            [str(binary), "--profile-id", "package-smoke", "--subject-id", "package-smoke-user",
             "--agent-id", "sora", "--client-instance-id", "package-smoke-client",
             "--origin", "tauri://localhost", "--no-legacy-migration"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if fenced.returncode != 5:
            raise RuntimeError("erased_identity_not_fenced")
        report["erased_identity_startup_exit"] = fenced.returncode
        report["status"] = "passed"
    except Exception as error:
        # Never serialize exception messages, bootstrap capabilities or response bodies.
        report.update(status="failed", error_type=type(error).__name__)
    finally:
        if process.poll() is None:
            try:
                if request is not None:
                    request("/v1/admin/shutdown", method="POST")
                    process.wait(timeout=20)
            except Exception:
                pass
            if process.poll() is None:
                # One-file PyInstaller runs a child. Limit cleanup to this test tree.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, check=False,
                )
                process.wait(timeout=10)
        reader.join(timeout=5)
        process.stdout.close()
        if native_fixture is not None:
            native_fixture.unlink(missing_ok=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    binary = args.gateway.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="sora-package-smoke-") as temporary:
        report = verify(binary, temporary)
    encoded = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
