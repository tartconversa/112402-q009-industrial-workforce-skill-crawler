"""便捷入口：``from app import ParkService``。"""

from parkbridge import *  # noqa: F401,F403
from parkbridge import ParkService, cli  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(cli.main())
