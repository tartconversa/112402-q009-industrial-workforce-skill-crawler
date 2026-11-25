"""scraper 命名兼容入口。"""

from collector import *  # noqa: F401,F403
from parkbridge.collectors import Collector as Scraper

__all__ = ["Scraper"]
