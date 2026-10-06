"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .quota import QuotaService
from .service import DomainService
from .storage import Database


def _quota(service: DomainService) -> QuotaService:
    """复用同一数据库与审计链，惰性创建配额协调服务。"""

    cached = getattr(service, "_quota_service", None)
    if cached is None:
        cached = QuotaService(service.database, service.clock)
        setattr(service, "_quota_service", cached)
    return cached


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        status, payload = _route_quota(_quota(service), method, parsed.path, query,
                                       body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt_status(receipt) -> tuple[int, dict[str, Any]]:
    return 200 if receipt.replayed else 201, receipt.__dict__


def _route_quota(quota: QuotaService, method: str, path: str,
                 query: dict[str, list[str]], body: dict[str, Any],
                 actor_id: str) -> tuple[int | None, dict[str, Any]]:
    """分派配额协调相关路由。"""

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    if method == "POST":
        if path == "/teams":
            return _receipt_status(quota.register_team(actor_id=actor_id, **body))
        if path == "/tasks":
            return _receipt_status(quota.register_task(actor_id=actor_id, **body))
        if path == "/resources":
            return _receipt_status(quota.register_resource(actor_id=actor_id, **body))
        if path == "/resource-windows":
            return _receipt_status(quota.declare_window(actor_id=actor_id, **body))
        if path == "/resource-failures":
            return _receipt_status(quota.report_resource_failure(actor_id=actor_id, **body))
        if path == "/applications":
            return _receipt_status(quota.request_allocation(actor_id=actor_id, **body))
        if path == "/applications/reschedule":
            return _receipt_status(quota.reschedule_application(actor_id=actor_id, **body))
        if path == "/applications/cancel":
            return _receipt_status(quota.cancel_application(actor_id=actor_id, **body))
        if path == "/applications/start":
            return _receipt_status(quota.mark_started(actor_id=actor_id, **body))
        if path == "/applications/failover":
            return _receipt_status(quota.failover_run(actor_id=actor_id, **body))
        if path == "/recovery":
            plan = quota.run_recovery()
            return 200, {"plan": None if plan is None else plan.__dict__}
        if path == "/preview/window-change":
            return 200, quota.preview_window_change(**body)
        if path == "/preview/cancel":
            return 200, quota.preview_cancel(**body)
    if method == "GET":
        if path == "/resources":
            return 200, {"items": [item.__dict__ for item in quota.list_resources(q("pool_id"))]}
        if path == "/applications":
            return 200, {"items": quota.list_applications(q("team_id"), q("status"))}
        if path.startswith("/applications/"):
            application_id = path.rsplit("/", 1)[1]
            return 200, quota.get_application(application_id)
        if path == "/quota-view":
            resource_id = q("resource_id", "")
            start_at = q("start_at", "")
            end_at = q("end_at", "")
            if not resource_id or not start_at or not end_at:
                raise ValidationError("resource_id、start_at 与 end_at 均不能为空")
            return 200, quota.quota_view(resource_id, start_at, end_at)
        if path == "/plans/latest":
            plan = quota.latest_plan()
            return 200, {"plan": None if plan is None else plan.__dict__}
    return None, {}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动科技战略协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
