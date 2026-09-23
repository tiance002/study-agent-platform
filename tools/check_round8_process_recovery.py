"""Exercise real PostgreSQL-backed web and worker process recovery locally."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(BACKEND_ROOT / "tests"))

import pg_support  # noqa: E402


@dataclass
class ManagedProcess:
    process: subprocess.Popen
    log_stream: object


class ProcessTestProvider:
    def __init__(self, *, block_marker: Path | None = None) -> None:
        self.block_marker = block_marker
        self.calls_path = Path(os.environ["R8_PROVIDER_CALLS"])

    def generate(self, request):
        from app.teaching.models import RawCitation
        from app.teaching.provider import completed_result

        self.calls_path.parent.mkdir(parents=True, exist_ok=True)
        with self.calls_path.open("a", encoding="ascii") as stream:
            stream.write(request.attempt_id + "\n")
        if self.block_marker is not None:
            self.block_marker.parent.mkdir(parents=True, exist_ok=True)
            self.block_marker.write_text("provider_started", encoding="ascii")
            release_path = Path(os.environ["R8_PROVIDER_RELEASE"])
            while not release_path.exists():
                time.sleep(0.05)

        citations = ()
        if request.artifacts:
            artifact = request.artifacts[0]
            citations = (
                RawCitation(
                    source_id=artifact.source_id,
                    document_id=artifact.document_id,
                    span_start=artifact.span_start,
                    span_end=artifact.span_end,
                    content_hash=artifact.content_hash,
                ),
            )
        return completed_result(
            attempt_id=request.attempt_id,
            answer="进程恢复后仍能返回准确引用。",
            citations=citations,
            provider_request_id="round8-local-" + request.attempt_id,
        )


def _worker_main() -> int:
    from app.deployment import DeploymentSettings
    from app.main import build_platform
    from app.workers.ingestion import run_once as run_ingestion_once
    from app.workers.teaching import run_once as run_teaching_once

    platform = build_platform(
        var_dir=Path(os.environ["R8_PROCESS_VAR_DIR"]),
        settings=DeploymentSettings.load(),
    )
    mode = os.environ.get("R8_WORKER_MODE", "normal")
    marker = Path(os.environ["R8_PROVIDER_STARTED"]) if mode == "block" else None
    platform.teaching_provider = ProcessTestProvider(block_marker=marker)
    ready = Path(os.environ["R8_WORKER_READY_FILE"])
    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.write_text("ready", encoding="ascii")
    worker_id = os.environ.get("R8_WORKER_ID", "round8-process-worker")
    lease_seconds = int(os.environ.get("R8_LEASE_SECONDS", "5"))

    while True:
        run_ingestion_once(platform, worker_id=worker_id, lease_seconds=lease_seconds)
        outcome = run_teaching_once(
            platform, worker_id=worker_id, lease_seconds=lease_seconds
        )
        if outcome != "idle":
            print(f"teaching_outcome={outcome}", flush=True)
        time.sleep(0.05)


def _web_main(port: int) -> int:
    import uvicorn
    from app.deployment import DeploymentSettings
    from app.main import build_platform, create_app

    platform = build_platform(
        var_dir=Path(os.environ["R8_PROCESS_VAR_DIR"]),
        settings=DeploymentSettings.load(),
    )
    uvicorn.run(
        create_app(platform=platform),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    return 0


def _spawn(command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path) -> ManagedProcess:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=stream,
        stderr=subprocess.STDOUT,
    )
    return ManagedProcess(process=process, log_stream=stream)


def _stop(handle: ManagedProcess | None, *, force: bool = False) -> None:
    if handle is None:
        return
    if handle.process.poll() is None:
        if force:
            handle.process.kill()
        else:
            handle.process.terminate()
        try:
            handle.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            handle.process.kill()
            handle.process.wait(timeout=5)
    handle.log_stream.close()


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _tail(path: Path) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
    except OSError:
        return ""


def _wait_until(predicate, *, description: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise RuntimeError(f"timeout waiting for {description}")


def _start_web(port: int, env: dict[str, str], log_path: Path) -> ManagedProcess:
    handle = _spawn(
        [sys.executable, str(Path(__file__).resolve()), "--web", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        log_path=log_path,
    )
    base_url = f"http://127.0.0.1:{port}"

    def healthy() -> bool:
        if handle.process.poll() is not None:
            raise RuntimeError(f"web process exited early:\n{_tail(log_path)}")
        try:
            return httpx.get(base_url + "/healthz", timeout=0.5).status_code == 200
        except httpx.HTTPError:
            return False

    try:
        _wait_until(healthy, description="web healthz")
    except Exception:
        _stop(handle, force=True)
        raise
    return handle


def _start_worker(
    *, env: dict[str, str], log_path: Path, worker_id: str, mode: str = "normal"
) -> ManagedProcess:
    ready_path = Path(env["R8_WORKER_READY_FILE"])
    ready_path.unlink(missing_ok=True)
    child_env = {**env, "R8_WORKER_ID": worker_id, "R8_WORKER_MODE": mode}
    handle = _spawn(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        cwd=REPO_ROOT,
        env=child_env,
        log_path=log_path,
    )
    try:
        _wait_until(
            lambda: ready_path.exists() or handle.process.poll() is not None,
            description=f"worker {worker_id} ready",
        )
        if handle.process.poll() is not None:
            raise RuntimeError(f"worker exited early:\n{_tail(log_path)}")
    except Exception:
        _stop(handle, force=True)
        raise
    return handle


def _expect(response: httpx.Response, status: int, label: str) -> dict:
    if response.status_code != status:
        raise AssertionError(f"{label}: expected {status}, got {response.status_code}: {response.text}")
    return response.json() if response.content else {}


def _wait_status(client: httpx.Client, path: str, expected: set[str], timeout: float = 25) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(path)
        last = _expect(response, 200, "poll durable state")
        if last.get("status") in expected:
            return last
        if last.get("status") in {"failed", "reconciliation_required"}:
            break
        time.sleep(0.1)
    raise AssertionError(f"status did not reach {sorted(expected)}: {last}")


def _proxy_drop_one_response(base_url: str, path: str, payload: dict, headers: dict) -> tuple[int, dict]:
    class DropResponseHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            forwarded_headers = {
                name: self.headers[name]
                for name in ("Cookie", "Origin", "Idempotency-Key", "Content-Type", "Accept")
                if self.headers.get(name) is not None
            }
            response = httpx.post(
                base_url + self.path,
                content=body,
                headers=forwarded_headers,
                timeout=10,
            )
            self.server.forwarded_status = response.status_code
            self.server.forwarded_body = response.json()
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(response.content) + 1))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

        def log_message(self, _format: str, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), DropResponseHandler)
    server.forwarded_status = None
    server.forwarded_body = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy_url = f"http://127.0.0.1:{server.server_port}"
    try:
        try:
            returned = httpx.post(
                proxy_url + path,
                json=payload,
                headers=headers,
                timeout=5,
            )
        except httpx.RemoteProtocolError:
            pass
        else:
            raise AssertionError(
                "response-dropping proxy unexpectedly returned an HTTP response: "
                f"status={returned.status_code}, headers={dict(returned.headers)}, body={returned.text!r}, "
                f"forwarded_status={server.forwarded_status}"
            )
        if server.forwarded_status is None or server.forwarded_body is None:
            raise AssertionError("proxy did not forward the committed request to web")
        return int(server.forwarded_status), dict(server.forwarded_body)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _configure_temp_budget(dsn: str) -> None:
    pg_support.require_test_database(dsn)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE platform_budget_config"
            " SET monthly_cap_micro = NULL, paid_dispatch_enabled = true"
            " WHERE config_id = true"
        )
        connection.commit()


def _database_snapshot(dsn: str, run_id: str) -> dict[str, object]:
    pg_support.require_test_database(dsn)
    from app.budget.platform import reservation_id_for_run

    with psycopg.connect(dsn) as connection:
        run_count = connection.execute(
            "SELECT count(*) FROM teaching_runs WHERE run_id = %s", (run_id,)
        ).fetchone()[0]
        attempts = connection.execute(
            "SELECT status FROM provider_attempts WHERE run_id = %s ORDER BY created_at",
            (run_id,),
        ).fetchall()
        teaching_reservation = connection.execute(
            "SELECT state, actual_micro FROM teaching_reservations WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        platform_reservation = connection.execute(
            "SELECT state, actual_spend_micro FROM platform_paid_reservations"
            " WHERE reservation_id = %s",
            (reservation_id_for_run(run_id),),
        ).fetchone()
    return {
        "run_count": int(run_count),
        "attempts": [str(row[0]) for row in attempts],
        "teaching_reservation": tuple(teaching_reservation) if teaching_reservation else None,
        "platform_reservation": tuple(platform_reservation) if platform_reservation else None,
    }


def _controller(artifact_root: Path) -> int:
    if not pg_support.reachable():
        print("ROUND8_PROCESS_RECOVERY_BLOCKED: PostgreSQL is not reachable.")
        return 2

    artifact_root.mkdir(parents=True, exist_ok=True)
    run_dir = artifact_root / ("run-" + uuid.uuid4().hex[:10])
    run_dir.mkdir(parents=True)
    database = pg_support.create_test_database()
    web: ManagedProcess | None = None
    worker: ManagedProcess | None = None
    try:
        _configure_temp_budget(database.migration_dsn)
        port = _port()
        base_url = f"http://127.0.0.1:{port}"
        var_root = run_dir / "state"
        calls_path = run_dir / "provider-calls.txt"
        marker_path = run_dir / "provider-started"
        release_path = run_dir / "provider-release"
        ready_path = run_dir / "worker-ready"
        env = os.environ.copy()
        env.update(database.env())
        env.update(
            {
                "STUDY_PLATFORM_ENV": "development",
                "STUDY_PLATFORM_PERSISTENCE": "postgres",
                "STUDY_PLATFORM_SESSION_SECRET": secrets.token_hex(32),
                "STUDY_PLATFORM_COOKIE_SECRET": secrets.token_hex(32),
                "STUDY_PLATFORM_TOKEN_SECRET": secrets.token_hex(32),
                "STUDY_PLATFORM_COOKIE_SECURE": "false",
                "STUDY_PLATFORM_REGISTRATION_ENABLED": "true",
                "STUDY_PLATFORM_PASSWORD_LOGIN_ENABLED": "true",
                "STUDY_PLATFORM_EXCHANGE_LIMIT": "10000",
                "STUDY_PLATFORM_TEACHING_PROVIDER": "openai",
                "STUDY_PLATFORM_TEACHING_MODEL": "round8-process-test-model",
                "STUDY_PLATFORM_TEACHING_API_KEY": "local-fixture-not-a-real-key",
                "STUDY_PLATFORM_TEACHING_BASE_URL": "http://127.0.0.1:1/v1",
                "STUDY_PLATFORM_PAID_DISPATCH_ENABLED": "true",
                "R8_PROCESS_VAR_DIR": str(var_root),
                "R8_PROVIDER_CALLS": str(calls_path),
                "R8_PROVIDER_STARTED": str(marker_path),
                "R8_PROVIDER_RELEASE": str(release_path),
                "R8_WORKER_READY_FILE": str(ready_path),
                "R8_LEASE_SECONDS": "5",
            }
        )

        web = _start_web(port, env, run_dir / "web-before.log")
        worker = _start_worker(
            env=env,
            log_path=run_dir / "worker-before.log",
            worker_id="round8-before-restart",
        )
        headers = {"Idempotency-Key": "r8-process-" + uuid.uuid4().hex}
        password = "Round8-process-password-123"
        username = "r8p" + uuid.uuid4().hex[:9]

        with httpx.Client(
            base_url=base_url,
            headers={"Origin": base_url},
            timeout=5,
            follow_redirects=False,
        ) as client:
            registered = _expect(
                client.post("/auth/register", json={"username": username, "password": password}),
                201,
                "register cookie user",
            )
            principal_id = registered["principal_id"]
            project_id = registered["default_project_id"]
            conversation = _expect(
                client.post(
                    f"/projects/{project_id}/conversations",
                    json={"title": "真实进程重启恢复"},
                    headers={**headers, "Idempotency-Key": "r8-conversation-" + uuid.uuid4().hex},
                ),
                201,
                "create conversation",
            )
            conversation_id = conversation["conversation_id"]
            _expect(
                client.put(
                    f"/projects/{project_id}/plan",
                    json={
                        "goal": "验证真实 web 与 worker 进程恢复",
                        "milestones": [{"title": "重启后资料与引用仍可读", "description": "", "tasks": []}],
                    },
                    headers={**headers, "Idempotency-Key": "r8-plan-" + uuid.uuid4().hex},
                ),
                200,
                "save plan",
            )
            source = _expect(
                client.post(
                    f"/projects/{project_id}/sources",
                    json={"display_name": "重启恢复资料", "acquisition": {"kind": "process-test"}},
                    headers={**headers, "Idempotency-Key": "r8-source-" + uuid.uuid4().hex},
                ),
                201,
                "register source",
            )
            source_id = source["source_id"]
            uploaded = _expect(
                client.post(
                    f"/projects/{project_id}/sources/{source_id}/content",
                    json={
                        "title": "进程重启资料",
                        "content": "重启后仍可读取这份资料，并保持准确引用。",
                        "media_type": "text/markdown",
                        "language": "zh",
                    },
                    headers={**headers, "Idempotency-Key": "r8-content-" + uuid.uuid4().hex},
                ),
                202,
                "enqueue source ingestion",
            )
            job_id = uploaded["job"]["job_id"]
            _wait_status(
                client,
                f"/projects/{project_id}/ingestion-jobs/{job_id}",
                {"succeeded"},
            )

            question = "重启后仍可读取这份资料吗？"
            run_key = "r8-main-run-" + uuid.uuid4().hex
            run_path = f"/projects/{project_id}/conversations/{conversation_id}/teaching-runs"
            created = _expect(
                client.post(run_path, json={"question": question}, headers={**headers, "Idempotency-Key": run_key}),
                202,
                "queue teaching run",
            )
            run_id = created["run_id"]
            run_status_path = f"/projects/{project_id}/teaching-runs/{run_id}"
            finished = _wait_status(client, run_status_path, {"succeeded"})
            if not finished["citations"]:
                raise AssertionError("deterministic provider did not return a citation")
            citation = finished["citations"][0]
            span = _expect(
                client.get(
                    f"/projects/{project_id}/sources/{citation['source_id']}/span",
                    params={
                        "document_id": citation["document_id"],
                        "start": citation["span_start"],
                        "end": citation["span_end"],
                        "content_hash": citation["content_hash"],
                    },
                ),
                200,
                "read exact citation span",
            )
            if span.get("content") != "重启后仍可读取这份资料，并保持准确引用。":
                raise AssertionError(f"citation span mismatch: {span}")
            main_snapshot = _database_snapshot(database.migration_dsn, run_id)
            if (
                main_snapshot["run_count"] != 1
                or main_snapshot["attempts"] != ["completed"]
                or main_snapshot["teaching_reservation"][0] != "settled"
                or main_snapshot["platform_reservation"][0] != "settled"
            ):
                raise AssertionError(f"unexpected settled run snapshot: {main_snapshot}")

            _stop(worker)
            worker = None
            _stop(web)
            web = None
            env["R8_PROCESS_VAR_DIR"] = str(var_root / "web-restarted")
            web = _start_web(port, env, run_dir / "web-after.log")
            env["R8_PROCESS_VAR_DIR"] = str(var_root / "worker-restarted")
            worker = _start_worker(
                env=env,
                log_path=run_dir / "worker-after.log",
                worker_id="round8-after-restart",
            )

            me = _expect(client.get("/me"), 200, "cookie session after process restart")
            if me["principal_id"] != principal_id:
                raise AssertionError("cookie principal changed after web restart")
            replay = client.post(
                run_path,
                json={"question": question},
                headers={**headers, "Idempotency-Key": run_key},
            )
            replay_body = _expect(replay, 202, "idempotency replay after restart")
            if replay_body["run_id"] != run_id or replay.headers.get("X-Idempotent-Replay") != "true":
                raise AssertionError("persisted HTTP idempotency did not replay the original run")
            if client.get(f"/projects/{project_id}/plan").status_code != 200:
                raise AssertionError("plan missing after process restart")
            if client.get(f"/projects/{project_id}/ingestion-jobs/{job_id}").json()["status"] != "succeeded":
                raise AssertionError("ingestion status missing after process restart")
            messages = _expect(
                client.get(f"/projects/{project_id}/conversations/{conversation_id}/messages"),
                200,
                "messages after process restart",
            )["messages"]
            if [message["role"] for message in messages] != ["user", "assistant"]:
                raise AssertionError(f"messages were not durable across restart: {messages}")
            sources = _expect(client.get(f"/projects/{project_id}/sources"), 200, "sources after restart")["sources"]
            if not any(item["source_id"] == source_id for item in sources):
                raise AssertionError("source metadata missing after process restart")

            lost_key = "r8-lost-response-" + uuid.uuid4().hex
            lost_payload = {"question": "丢失 HTTP 响应后也只能创建一次运行。"}
            lost_headers = {
                "Cookie": "study_session=" + client.cookies.get("study_session"),
                "Origin": base_url,
                "Idempotency-Key": lost_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            forwarded_status, forwarded_body = _proxy_drop_one_response(
                base_url, run_path, lost_payload, lost_headers
            )
            if forwarded_status != 202:
                raise AssertionError(f"response-dropping proxy got unexpected upstream status {forwarded_status}")
            lost_replay = client.post(
                run_path,
                json=lost_payload,
                headers={**headers, "Idempotency-Key": lost_key},
            )
            lost_body = _expect(lost_replay, 202, "retry after dropped response")
            if (
                lost_body["run_id"] != forwarded_body["run_id"]
                or lost_replay.headers.get("X-Idempotent-Replay") != "true"
            ):
                raise AssertionError("lost response retry created a different teaching run")
            lost_run_id = lost_body["run_id"]
            _wait_status(client, f"/projects/{project_id}/teaching-runs/{lost_run_id}", {"succeeded"})
            if _database_snapshot(database.migration_dsn, lost_run_id)["run_count"] != 1:
                raise AssertionError("lost response duplicate created more than one run")

            _stop(worker)
            worker = None
            unknown_key = "r8-unknown-result-" + uuid.uuid4().hex
            unknown_payload = {"question": "已派发但没有响应时不能重复调用 provider。"}
            unknown_body = _expect(
                client.post(
                    run_path,
                    json=unknown_payload,
                    headers={**headers, "Idempotency-Key": unknown_key},
                ),
                202,
                "queue unknown-result test run",
            )
            unknown_run_id = unknown_body["run_id"]
            marker_path.unlink(missing_ok=True)
            env["R8_PROCESS_VAR_DIR"] = str(var_root / "worker-blocked")
            worker = _start_worker(
                env=env,
                log_path=run_dir / "worker-blocked.log",
                worker_id="round8-blocked-worker",
                mode="block",
            )
            _wait_until(lambda: marker_path.exists(), description="provider call entered before worker kill")
            calls_before_kill = calls_path.read_text(encoding="ascii").splitlines()
            if not calls_before_kill:
                raise AssertionError("blocking provider did not record its call")
            _stop(worker, force=True)
            worker = None

            pg_support.require_test_database(database.migration_dsn)
            with psycopg.connect(database.migration_dsn) as connection:
                connection.execute(
                    "UPDATE teaching_runs SET lease_until = now() - interval '1 second'"
                    " WHERE run_id = %s AND status = 'running'",
                    (unknown_run_id,),
                )
                connection.commit()
            env["R8_PROCESS_VAR_DIR"] = str(var_root / "worker-recovered")
            worker = _start_worker(
                env=env,
                log_path=run_dir / "worker-recovered.log",
                worker_id="round8-recovery-worker",
            )
            recovered = _wait_status(
                client,
                f"/projects/{project_id}/teaching-runs/{unknown_run_id}",
                {"reconciliation_required"},
            )
            calls_after_recovery = calls_path.read_text(encoding="ascii").splitlines()
            if len(calls_after_recovery) != len(calls_before_kill):
                raise AssertionError("recovered worker called the uncertain provider attempt again")
            unknown_snapshot = _database_snapshot(database.migration_dsn, unknown_run_id)
            if (
                unknown_snapshot["run_count"] != 1
                or unknown_snapshot["attempts"] != ["unknown"]
                or unknown_snapshot["teaching_reservation"][0] != "in_flight"
                or unknown_snapshot["platform_reservation"][0] != "in_flight"
                or recovered["status"] != "reconciliation_required"
            ):
                raise AssertionError(f"unknown outcome was not held for reconciliation: {unknown_snapshot}")

        summary = {
            "status": "passed",
            "database": database.name,
            "web_worker_restart": "cookie, project, conversation, plan, source, ingestion, messages, run, and citation persisted",
            "dropped_response": "same idempotency key replayed one committed run",
            "killed_worker": "dispatched provider attempt recovered as reconciliation_required without retry",
            "run_ids": {"settled": run_id, "dropped_response": lost_run_id, "unknown": unknown_run_id},
        }
        (run_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"ROUND8_PROCESS_RECOVERY_PASSED: {run_dir}")
        return 0
    except Exception as error:
        print(f"ROUND8_PROCESS_RECOVERY_FAILED: {type(error).__name__}: {error}")
        print(f"PROCESS_ARTIFACTS: {run_dir}")
        return 1
    finally:
        _stop(worker, force=worker is not None and worker.process.poll() is None)
        _stop(web, force=web is not None and web.process.poll() is None)
        pg_support.drop_test_database(database)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--artifacts", default=str(REPO_ROOT / "var" / "round8-process-recovery"))
    args = parser.parse_args()
    if args.worker:
        return _worker_main()
    if args.web:
        return _web_main(args.port)
    if args.port:
        parser.error("--port is only valid with --web")
    return _controller(Path(args.artifacts))


if __name__ == "__main__":
    raise SystemExit(main())
