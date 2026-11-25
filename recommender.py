"""推荐引擎兼容入口。"""

from parkbridge.ontology import SkillGovernance
from parkbridge.service import ParkService


class Recommender:
    """薄封装：也可直接使用 :class:`parkbridge.ParkService`。"""

    def __init__(self, service: ParkService):
        self.service = service

    def recommend(self, job_id: str, **kwargs):
        return self.service.recommend_for_job(job_id, **kwargs)

    def create_recommendation(self, *args, **kwargs):
        return self.service.create_recommendation(*args, **kwargs)


__all__ = ["Recommender", "SkillGovernance", "ParkService"]
