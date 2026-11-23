"""CLI: ``serve`` (API + worker), ``index-folder`` (one-shot bulk load), ``gateway``,
``xnat-access-sync`` (Keycloak project groups -> XNAT project roles), ``auto-run`` (launch
pipelines on studies as they arrive), ``dicom-export`` (AI results in XNAT -> a DICOM
destination), ``loadtest`` (synthetic index-only data for scale
testing)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from scoutoss_common.logging import configure_logging
from scoutoss_indexer import SERVICE_NAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scoutoss-indexer")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the HTTP API and polling worker")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container listener
    serve.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    folder = sub.add_parser("index-folder", help="index every DICOM file under a folder once")
    folder.add_argument("path", type=Path)
    folder.add_argument("--project", default=os.getenv("DEFAULT_PROJECT", "FOLDER"))
    sub.add_parser("gateway", help="run the DICOM de-identification gateway")
    sub.add_parser("xnat-access-sync", help="mirror Keycloak project groups into XNAT roles")
    auto = sub.add_parser("auto-run", help="launch pipelines on studies as they arrive")
    auto.add_argument("--status", action="store_true", help="print what was launched")
    export = sub.add_parser("dicom-export", help="send AI results in XNAT to a DICOM destination")
    export.add_argument("--status", action="store_true", help="print what has been sent")
    export.add_argument("--verify", action="store_true", help="check the destination has it")
    loadtest = sub.add_parser("loadtest", help="index synthetic studies for scale testing")
    loadtest.add_argument("--studies", type=int, default=10_000)
    loadtest.add_argument("--project", default="LOADTEST")
    loadtest.add_argument("--seed", type=int, default=7)
    loadtest.add_argument("--purge", action="store_true", help="remove the project's load data")
    args = parser.parse_args(argv)

    if args.cmd == "xnat-access-sync":
        from scoutoss_indexer.xnat_access import main as access_main

        return access_main()

    if args.cmd == "auto-run":
        from scoutoss_indexer.auto_run import main as auto_main

        return auto_main(["--status"] if args.status else [])

    if args.cmd == "dicom-export":
        from scoutoss_indexer.dicom_export import main as export_main

        return export_main([f"--{flag}" for flag in ("status", "verify") if getattr(args, flag)])

    if args.cmd == "gateway":
        from scoutoss_indexer.gateway import main as gateway_main

        return gateway_main()

    configure_logging(SERVICE_NAME)

    if args.cmd == "serve":  # pragma: no cover - process entry point
        import uvicorn

        from scoutoss_common.tracing import setup_tracing
        from scoutoss_indexer.app import create_app

        app = create_app()
        setup_tracing(SERVICE_NAME, app)
        uvicorn.run(app, host=args.host, port=args.port, log_config=None, access_log=False)
        return 0

    from scoutoss_indexer.bootstrap import build_store, build_vault
    from scoutoss_indexer.pipeline import Pipeline
    from scoutoss_indexer.settings import get_settings
    from scoutoss_indexer.sources.folder import FolderSource

    settings = get_settings()
    store = build_store(settings)
    store.ensure_indices()
    if args.cmd == "loadtest":
        from scoutoss_indexer import loadgen

        if args.purge:
            print(json.dumps({"purged": loadgen.purge(store.client, args.project)}))
        else:
            stats = loadgen.load(store, args.studies, project=args.project, seed=args.seed)
            print(json.dumps(stats.as_dict()))
        return 0
    pipeline = Pipeline(
        store, salt=settings.salt_bytes(), vault=build_vault(settings), default_project=args.project
    )
    result = pipeline.process(FolderSource(args.path, project=args.project).pending())
    print(
        json.dumps(
            {
                "indexed_series": result.indexed,
                "studies": len(result.studies),
                "patients": len(result.patients),
                "dead_lettered": result.dead_lettered,
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
