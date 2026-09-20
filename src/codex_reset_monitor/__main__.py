"""支持 ``python -m codex_reset_monitor`` 启动 CLI。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
