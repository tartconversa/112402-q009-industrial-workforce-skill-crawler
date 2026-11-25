"""一键演示/流水线入口。"""

from pathlib import Path

from parkbridge import ParkService
from parkbridge.sample import import_samples


def build_sample_database(path: str = "data/parkbridge.sqlite3") -> dict:
    """创建文件数据库并导入样例（重复调用会按样例 ID 更新）。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    service = ParkService.open(path)
    try:
        return import_samples(service)
    finally:
        service.close()


def in_memory_sample_service() -> ParkService:
    service = ParkService.in_memory()
    import_samples(service)
    return service


if __name__ == "__main__":
    print(build_sample_database())
