"""CLI: ``scoutoss-copilot serve`` runs the API; ``ask "question"`` answers once (offline)."""

from __future__ import annotations

import argparse
import os
import sys

from scoutoss_common.logging import configure_logging
from scoutoss_copilot import SERVICE_NAME


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    parser = argparse.ArgumentParser(prog="scoutoss-copilot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container listener
    serve.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    plan = sub.add_parser("plan", help="print the plan the configured provider generates")
    plan.add_argument("question")
    args = parser.parse_args(argv)
    configure_logging(SERVICE_NAME)
    if args.cmd == "plan":
        import json

        from scoutoss_copilot.clients import static_schema
        from scoutoss_copilot.llm import RedactingLLM, build_provider
        from scoutoss_copilot.plan import validate_plan
        from scoutoss_copilot.prompt import build_messages
        from scoutoss_copilot.settings import get_settings

        llm = RedactingLLM(build_provider(get_settings()))
        raw = llm.complete(build_messages(args.question, static_schema()))
        print(json.dumps(validate_plan(raw).model_dump(exclude_none=True), indent=2))
        return 0
    import uvicorn

    from scoutoss_common.tracing import setup_tracing
    from scoutoss_copilot.app import create_app

    app = create_app()
    setup_tracing(SERVICE_NAME, app)
    uvicorn.run(app, host=args.host, port=args.port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
