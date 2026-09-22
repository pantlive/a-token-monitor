"""支持 ``python -m token_monitor`` 启动 CLI。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
