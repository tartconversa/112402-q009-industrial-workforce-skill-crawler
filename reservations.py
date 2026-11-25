"""席位预留兼容入口。"""

from parkbridge.errors import CapacityExceededError, DuplicateLearnerConfirmationError
from parkbridge.service import ParkService


class ReservationService:
    def __init__(self, service: ParkService):
        self.service = service

    def hold(self, referral_id: str, seats: int | None = None, **kwargs):
        return self.service.hold_reservation(referral_id, seats=seats, **kwargs)

    def confirm(self, referral_id: str, **kwargs):
        return self.service.confirm_referral(referral_id, **kwargs)

    def release(self, referral_id: str, **kwargs):
        return self.service.release_referral(referral_id, **kwargs)

    def free_seats(self, course_id: str):
        return self.service.course_occupancy(course_id)["free_seats"]


__all__ = [
    "ReservationService",
    "CapacityExceededError",
    "DuplicateLearnerConfirmationError",
    "ParkService",
]
