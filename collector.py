"""采集器兼容模块名；完整实现见 :mod:`parkbridge.collectors`。"""

from parkbridge.collectors import (
    AccessPolicy,
    Collector,
    CsvAdapter,
    DictTransport,
    HttpFetcher,
    JsonPagedAdapter,
    SourceAdapter,
    UrllibTransport,
)

__all__ = [
    "AccessPolicy",
    "Collector",
    "CsvAdapter",
    "DictTransport",
    "HttpFetcher",
    "JsonPagedAdapter",
    "SourceAdapter",
    "UrllibTransport",
]
