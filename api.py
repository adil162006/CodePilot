from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse

from groq import RateLimitError
from langgraph.types import Command

from agents.code_pilot_agent import code_pilot_graph
from models.code_pilot_state import CodePilotState


PROJECT_ROOT = Path(__file__).resolve().parent
UPLOAD_ROOT = PROJECT_ROOT / "data" / "agent_projects"
MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_FILES = 100
MAX_RATE_LIMIT_RETRIES = 5
MAX_OUTPUT_FILE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 10 * 1024 * 1024
IGNORED_OUTPUT_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}

JOB_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="codepilot-job",
)


@dataclass
class WorkflowJob:
    job_id: str
    project_dir: Path
    files: list[str]
    status: str = "queued"
    stage: str = "queued"
    messages: list[dict[str, Any]] = field(default_factory=list)
    response: str = ""
    error: str | None = None
    approval_request: dict[str, Any] | None = None
    approval_response: dict[str, Any] | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)


JOBS: dict[str, WorkflowJob] = {}
JOBS_LOCK = threading.Lock()


def _rate_limit_wait_seconds(error: RateLimitError, attempt: int) -> float:
    """Use Groq's suggested delay, falling back to exponential backoff."""
    match = re.search(
        r"try again in\s+([\d.]+)\s*s",
        str(error),
        re.IGNORECASE,
    )
    if match:
        return min(float(match.group(1)) + 0.5, 300.0)

    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    retry_after = headers.get("retry-after") if headers else None
    if retry_after:
        try:
            return min(float(retry_after) + 0.5, 300.0)
        except ValueError:
            pass

    return min(5.0 * (2 ** (attempt - 1)), 60.0)


def _normalize_uploads(raw_files: Any) -> list[tuple[Path, str]]:
    """Validate JSON file entries and return safe relative paths and text."""
    if isinstance(raw_files, dict):
        entries = [
            {"path": path, "content": content}
            for path, content in raw_files.items()
        ]
    elif isinstance(raw_files, list):
        entries = raw_files
    else:
        raise ValueError("'files' must be a list or a path-to-content object.")

    if not entries:
        raise ValueError("Upload at least one file.")
    if len(entries) > MAX_UPLOAD_FILES:
        raise ValueError(f"Upload no more than {MAX_UPLOAD_FILES} files.")

    validated: list[tuple[Path, str]] = []
    seen_paths: set[str] = set()
    total_bytes = 0

    for entry in entries:
        if isinstance(entry, dict):
            raw_path = entry.get("path", entry.get("relative_path"))
            content = entry.get("content")
        else:
            raw_path = None
            content = None

        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError("Each file needs a non-empty relative 'path'.")
        if not isinstance(content, str):
            raise ValueError(f"File content for {raw_path!r} must be text.")

        normalized = raw_path.strip().replace("\\", "/")
        windows_path = PureWindowsPath(raw_path)
        parts = normalized.split("/")
        if (
            normalized.startswith("/")
            or windows_path.is_absolute()
            or windows_path.drive
            or any(":" in part for part in parts)
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(f"Unsafe relative file path: {raw_path!r}.")

        relative_path = Path(*PurePosixPath(normalized).parts)
        canonical_path = relative_path.as_posix().casefold()
        if canonical_path in seen_paths:
            raise ValueError(f"Duplicate file path: {raw_path!r}.")
        seen_paths.add(canonical_path)

        file_bytes = len(content.encode("utf-8"))
        total_bytes += file_bytes
        if total_bytes > MAX_REQUEST_BYTES:
            raise ValueError("Combined uploaded file contents exceed 10 MB.")

        validated.append((relative_path, content))

    return validated


def _get_job(job_id: str) -> WorkflowJob | None:
    with JOBS_LOCK:
        return JOBS.get(job_id)


def _set_job(job: WorkflowJob, **updates: Any) -> None:
    with job.condition:
        for key, value in updates.items():
            setattr(job, key, value)
        job.condition.notify_all()


def _api_message(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        message_type = message.get("type", "message")
        content = message.get("content", message)
    else:
        message_type = getattr(message, "type", type(message).__name__)
        content = getattr(message, "content", str(message))

    # Ensure the response can be returned as JSON even for structured messages.
    try:
        json.dumps(content)
    except (TypeError, ValueError):
        content = str(content)

    return {"type": message_type, "content": content}


def _project_files(project_dir: Path) -> list[str]:
    paths: list[str] = []
    root = project_dir.resolve()

    for path in project_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(project_dir)
        if IGNORED_OUTPUT_DIRS.intersection(relative.parts):
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        paths.append(relative.as_posix())

    return sorted(paths)


def _job_payload(job: WorkflowJob) -> dict[str, Any]:
    with job.condition:
        return {
            "job_id": job.job_id,
            "status": job.status,
            "stage": job.stage,
            "messages": job.messages,
            "response": job.response,
            "error": job.error,
            "files": list(job.files),
            "approval_request": job.approval_request,
        }


def _wait_for_approval(
    job: WorkflowJob,
    request: dict[str, Any],
) -> dict[str, Any]:
    with job.condition:
        job.status = "awaiting_approval"
        job.stage = "debugger"
        job.approval_request = request
        job.approval_response = None
        job.condition.notify_all()

        while job.approval_response is None:
            job.condition.wait()

        response = job.approval_response
        job.approval_request = None
        job.approval_response = None
        job.status = "running"
        job.condition.notify_all()
        return response


def _run_job(job: WorkflowJob) -> None:
    _set_job(job, status="running", stage="debugger")
    config = {
        "recursion_limit": 50,
        "configurable": {"thread_id": f"api-job-{job.job_id}"},
    }
    pending_input: Any = CodePilotState(
        project_root=str(job.project_dir),
        is_code_quality_agent=True,
        is_documentation_agent=True,
    )
    rate_limit_retries = 0
    stage_names = {
        "debugger_agent_node": "debugger",
        "code_quality_agent_node": "code_quality",
        "documentation_agent_node": "documentation",
    }

    try:
        while True:
            interrupted = False
            try:
                for event in code_pilot_graph.stream(
                    pending_input,
                    config=config,
                    stream_mode="updates",
                ):
                    for node_name, stage in stage_names.items():
                        if node_name in event:
                            rate_limit_retries = 0
                            _set_job(job, stage=stage)

                    interrupt_items = event.get("__interrupt__", ())
                    if interrupt_items:
                        request = interrupt_items[0].value
                        approval = _wait_for_approval(job, request)
                        pending_input = Command(resume=approval)
                        interrupted = True
                        break
            except RateLimitError as error:
                if rate_limit_retries >= MAX_RATE_LIMIT_RETRIES:
                    raise

                rate_limit_retries += 1
                wait_seconds = _rate_limit_wait_seconds(
                    error,
                    rate_limit_retries,
                )
                _set_job(job, status="retrying")
                time.sleep(wait_seconds)
                # Resume from the parent graph's last checkpoint. Completed
                # agents remain complete; only the failed graph step is retried.
                pending_input = None
                _set_job(job, status="running")
                continue

            if interrupted:
                continue
            break

        final_state = code_pilot_graph.get_state(config).values
        if hasattr(final_state, "model_dump"):
            final_state = final_state.model_dump()

        raw_messages = final_state.get("messages", [])
        messages = [_api_message(message) for message in raw_messages]
        response_parts = []
        for message in messages:
            content = message["content"]
            if isinstance(content, str) and content.strip():
                response_parts.append(content.strip())
            elif content:
                response_parts.append(json.dumps(content, ensure_ascii=False))

        _set_job(
            job,
            status="completed",
            stage="completed",
            messages=messages,
            response="\n\n".join(response_parts),
            files=_project_files(job.project_dir),
            error=None,
        )
    except Exception as error:
        _set_job(
            job,
            status="failed",
            stage="failed",
            error=f"{type(error).__name__}: {error}",
            files=_project_files(job.project_dir),
        )


class CodePilotAPI(BaseHTTPRequestHandler):
    server_version = "CodePilotAPI/1.0"

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        configured_origins = os.environ.get("CODEPILOT_CORS_ORIGINS", "")
        allowed_origins = {
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:63342",
            "http://127.0.0.1:63342",
        }
        allowed_origins.update(
            item.strip()
            for item in configured_origins.split(",")
            if item.strip()
        )
        is_local_dev_origin = False
        if origin:
            try:
                parsed_origin = urlparse(origin)
                is_local_dev_origin = (
                    parsed_origin.scheme == "http"
                    and parsed_origin.hostname in {"localhost", "127.0.0.1"}
                    and parsed_origin.port is not None
                    and not parsed_origin.username
                    and not parsed_origin.password
                    and not parsed_origin.path
                    and not parsed_origin.query
                    and not parsed_origin.fragment
                )
            except ValueError:
                # Malformed origins are not allowed.
                pass

        if origin in allowed_origins or is_local_dev_origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length header is required.")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValueError("Invalid Content-Length header.") from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise OverflowError("Request body must be no larger than 10 MB.")

        raw_body = self.rfile.read(length)
        try:
            return json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Request body must contain valid UTF-8 JSON.") from error

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/health":
            self._send_json({"status": "ok"})
            return

        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) < 3 or parts[:2] != ["api", "jobs"]:
            self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)
            return

        job = _get_job(parts[2])
        if job is None:
            self._send_json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
            return

        if len(parts) == 3:
            self._send_json(_job_payload(job))
            return

        if len(parts) == 4 and parts[3] == "files":
            payload = _job_payload(job)
            if payload["status"] not in {"completed", "failed"}:
                self._send_json(
                    {"error": "Files are available when the job finishes."},
                    HTTPStatus.CONFLICT,
                )
                return

            returned_files: list[dict[str, str]] = []
            omitted_files: list[str] = []
            total_bytes = 0
            for relative in payload["files"]:
                file_path = job.project_dir / Path(relative)
                try:
                    byte_count = file_path.stat().st_size
                    if byte_count > MAX_OUTPUT_FILE_BYTES:
                        omitted_files.append(relative)
                        continue
                    if total_bytes + byte_count > MAX_OUTPUT_TOTAL_BYTES:
                        omitted_files.append(relative)
                        continue
                    content = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    omitted_files.append(relative)
                    continue

                total_bytes += byte_count
                returned_files.append({"path": relative, "content": content})

            self._send_json({
                "job_id": job.job_id,
                "files": returned_files,
                "omitted_files": omitted_files,
            })
            return

        self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/jobs":
            self._create_job()
            return

        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "approval":
            self._submit_approval(parts[2])
            return

        self._send_json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def _create_job(self) -> None:
        try:
            body = self._read_json()
            uploads = _normalize_uploads(body.get("files") if isinstance(body, dict) else None)
        except OverflowError as error:
            self._send_json({"error": str(error)}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        job_id = uuid.uuid4().hex
        project_dir = UPLOAD_ROOT / job_id

        try:
            project_dir.mkdir(parents=True, exist_ok=False)
            project_root = project_dir.resolve()
            project_root.relative_to(UPLOAD_ROOT.resolve())
            for relative_path, content in uploads:
                destination = (project_dir / relative_path).resolve()
                destination.relative_to(project_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
        except (OSError, ValueError) as error:
            self._send_json(
                {"error": f"Could not save uploaded files: {error}"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        job = WorkflowJob(
            job_id=job_id,
            project_dir=project_dir,
            files=[relative.as_posix() for relative, _ in uploads],
        )
        with JOBS_LOCK:
            JOBS[job_id] = job

        JOB_EXECUTOR.submit(_run_job, job)
        self._send_json(
            {
                "job_id": job_id,
                "status": job.status,
                "status_url": f"/api/jobs/{job_id}",
                "files_url": f"/api/jobs/{job_id}/files",
            },
            HTTPStatus.ACCEPTED,
        )

    def _submit_approval(self, job_id: str) -> None:
        try:
            body = self._read_json()
        except OverflowError as error:
            self._send_json({"error": str(error)}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        except ValueError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return

        job = _get_job(job_id)
        if job is None:
            self._send_json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
            return
        if not isinstance(body, dict):
            self._send_json({"error": "Request body must be a JSON object."}, HTTPStatus.BAD_REQUEST)
            return

        if "approved_bug_identifiers" not in body:
            self._send_json(
                {"error": "Include 'approved_bug_identifiers' (use [] to reject all)."},
                HTTPStatus.BAD_REQUEST,
            )
            return

        requested = body["approved_bug_identifiers"]
        if (
            not isinstance(requested, list)
            or any(not isinstance(item, str) for item in requested)
        ):
            self._send_json(
                {"error": "'approved_bug_identifiers' must be a list of strings."},
                HTTPStatus.BAD_REQUEST,
            )
            return

        with job.condition:
            if job.status != "awaiting_approval" or not job.approval_request:
                self._send_json(
                    {"error": "This job is not waiting for patch approval."},
                    HTTPStatus.CONFLICT,
                )
                return

            reviewable = {
                patch.get("bug_identifier")
                for patch in job.approval_request.get("patches", [])
                if isinstance(patch, dict)
            }
            invalid = sorted(set(requested) - reviewable)
            if invalid:
                self._send_json(
                    {
                        "error": "Some identifiers are not in the pending patch list.",
                        "invalid_identifiers": invalid,
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return

            job.approval_response = {
                "approved_bug_identifiers": list(dict.fromkeys(requested))
            }
            job.condition.notify_all()

        self._send_json({"status": "approval_received", "job_id": job_id})

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")


def main() -> None:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("CODEPILOT_HOST", "127.0.0.1")
    port = int(os.environ.get("CODEPILOT_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), CodePilotAPI)
    server.daemon_threads = True

    print(f"CodePilot backend listening on http://{host}:{port}")
    print("Health check: GET /api/health")
    print("Create a workflow: POST /api/jobs")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping CodePilot backend...")
    finally:
        server.server_close()
        JOB_EXECUTOR.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
