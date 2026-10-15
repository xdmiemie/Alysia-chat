from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Alysia Studio desktop application")
    parser.add_argument("--character-id", default="", help="Character to restore on startup")
    parser.add_argument("--no-startup", action="store_true", help="Skip startup discovery (UI development)")
    parser.add_argument(
        "--smoke-test-ms",
        type=int,
        default=0,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from PySide6.QtCore import QTimer

        from desktop.app import configure_logging, create_application
        from desktop.controllers import AppController
        from desktop.windows import MainWindow
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6" or (exc.name or "").startswith("PySide6."):
            print(
                "缺少桌面依赖 PySide6。请运行：\n"
                "  python -m pip install -r desktop/requirements.txt",
                file=sys.stderr,
            )
            return 2
        raise

    configure_logging()
    app = create_application([sys.argv[0], *(argv or [])])
    controller = AppController()
    window = MainWindow(controller)
    window.show()
    if not args.no_startup:
        QTimer.singleShot(0, lambda: controller.start(args.character_id))
    if args.smoke_test_ms > 0:
        QTimer.singleShot(args.smoke_test_ms, window.close)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
