"""Run the application gateway, worker, local operations or stdio MCP endpoint."""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from pathlib import Path

from integrations.config import IntegrationConfig, PROJECT
from integrations.service import FinFlowService
from workflow.flywheel_config import FlywheelConfig


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=PROJECT / "config/integrations.json")
    parser.add_argument("--flywheel-config", type=Path, default=PROJECT / "config/flywheel.json")
    parser.add_argument("--data-root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--channel", choices=["feishu", "mcp"], default="feishu")
    commands.add_parser("status")
    report = commands.add_parser("report")
    report.add_argument("--date")
    run = commands.add_parser("run")
    run.add_argument("--no-discover", action="store_true")
    run.add_argument("--request-id")
    upload = commands.add_parser("import-pdf")
    upload.add_argument("path", type=Path)
    upload.add_argument("--request-id")
    job = commands.add_parser("job")
    job.add_argument("job_id")
    retry = commands.add_parser("retry")
    retry.add_argument("identity")
    retry.add_argument("--request-id")
    deliveries = commands.add_parser("deliveries")
    deliveries.add_argument("--status", choices=["pending", "sent", "failed", "rejected"], default="failed")
    deliveries.add_argument("--limit", type=int, default=20)
    retry_delivery = commands.add_parser("retry-delivery")
    retry_delivery.add_argument("delivery_id")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    commands.add_parser("feishu")
    commands.add_parser("mcp")
    args = parser.parse_args(argv)
    try:
        config = IntegrationConfig(args.config, flywheel=FlywheelConfig(args.flywheel_config, data_root=args.data_root))
        service, actor = FinFlowService(config), config.actor("local")
        if args.command == "mcp":
            from integrations.mcp_server import create_server

            create_server(service).run(transport="stdio")
            return 0
        if args.command in {"worker", "feishu"}:
            from integrations.runtime import Worker

            if args.command == "feishu":
                check = config.preflight(channel="feishu")
                if check["errors"]:
                    print(json.dumps(check, ensure_ascii=False, indent=2))
                    return 2
            worker = Worker(service)
            if getattr(args, "once", False):
                worker.ingress_once()
                worker.job_once()
                worker.queue_reports()
                worker.delivery_once()
                return 0
            stop = threading.Event()
            worker.serve(stop)
            try:
                if args.command == "feishu":
                    from integrations.channels.feishu import listen

                    listen(config, stop)
                else:
                    while not stop.wait(1):
                        pass
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()
            return 0
        if args.command == "preflight":
            result = config.preflight(channel=args.channel)
        elif args.command == "status":
            result = service.status(actor)
        elif args.command == "report":
            result = service.report(actor, args.date)
        elif args.command == "job":
            result = service.job(actor, args.job_id)
        elif args.command == "deliveries":
            result = service.deliveries(actor, args.status, args.limit)
        elif args.command == "retry-delivery":
            result = service.retry_delivery(actor, args.delivery_id)
        else:
            request_id = args.request_id or uuid.uuid4().hex
            if args.command == "run":
                result = service.start_run(actor, request_id, discover=not args.no_discover)
            elif args.command == "import-pdf":
                result = service.import_pdf(actor, args.path, request_id)
            else:
                result = service.retry(actor, args.identity, request_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "failed" else 0
    except Exception as exc:
        import sys

        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": "Check integration configuration, roles and optional dependencies",
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
