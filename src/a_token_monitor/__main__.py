"""支持 ``python -m a_token_monitor`` 启动 CLI。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
