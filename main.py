"""兼容直接运行 ``python main.py ...`` 的入口。"""

from parkbridge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
