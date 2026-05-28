"""Read-only behavior dimensions metadata.

M1: returns the 6 hard-coded behavior dimensions sentry-ai uses for risk
scoring. Mongolian labels + descriptions + default weights + active status.
M2 (Task #3) will accept per-camera weight overrides via PATCH.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/behaviors", tags=["behaviors"])


class BehaviorDimension(BaseModel):
    key: str
    label_mn: str
    description_mn: str
    default_weight: float
    active_in_m1: bool
    why_deferred: str | None = None


# Mirror sentry-ai's behavior.py DEFAULT_WEIGHTS. Single source of truth here
# until per-camera tuning (Task #3) makes the values dynamic.
_DIMENSIONS = [
    BehaviorDimension(
        key="looking_around",
        label_mn="Орчноо харах",
        description_mn=(
            "Нүүрний төв нь мөрний төвөөс хажуу талд эргэсэн. "
            "Хүн санаачилгатай эргэн харж байгаа гэсэн дохио."
        ),
        default_weight=1.5,
        active_in_m1=True,
    ),
    BehaviorDimension(
        key="item_pickup",
        label_mn="Бараа авах",
        description_mn=(
            "Бугуй тавиурын зоны эсвэл COCO-аар илрүүлсэн "
            "(шил/гар утас/түрийвч/laptop) барааны bbox дотор орсон. "
            "Шалгуурыг 'holding=True' болгоно."
        ),
        default_weight=15.0,
        active_in_m1=False,
        why_deferred=(
            "L4.5 өргөтгөл шаардлагатай — yolo11n.pt COCO детектор + "
            "камерын тавиурын zone polygon тохиргоо."
        ),
    ),
    BehaviorDimension(
        key="body_block",
        label_mn="Биеэр далдлах",
        description_mn=(
            "Мөрний өргөн нь rolling average-ийн 55%-аас бага. "
            "Хүн камер руу нуруугаа эргүүлж гар үйлдлийг далдалж байна."
        ),
        default_weight=3.0,
        active_in_m1=True,
    ),
    BehaviorDimension(
        key="crouch",
        label_mn="Бөхийх",
        description_mn=(
            "Бие/мөрнөөс хонго хүртэлх босоо урт нь хүний өндрийн 15%-аас "
            "багасч агшсан. Тавиур доор бараа авах эсвэл нуух дохио."
        ),
        default_weight=1.0,
        active_in_m1=True,
    ),
    BehaviorDimension(
        key="wrist_to_torso",
        label_mn="Хувцас доор нуух",
        description_mn=(
            "Бугуй хонгоны y-аас 15% дотор удаан хугацаагаар үлдсэн (8 frame "
            "тутамд score нэмэгдэнэ). Халаас/уут руу нуух дохио."
        ),
        default_weight=5.0,
        active_in_m1=False,
        why_deferred=(
            "Зөвхөн item_pickup-аас 'holding=True' болсон үед идэвхжинэ. "
            "L4.5 буюу холдогч тохиргооны дараа автомат ажиллана."
        ),
    ),
    BehaviorDimension(
        key="rapid_movement",
        label_mn="Хурдан хөдөлгөөн",
        description_mn=(
            "Бугуйн хурд нь хүний өндрийн 8%-аас илүү (frame тутмын зай). "
            "Бараа барьж байх үед эргэлзэх/нуух хурдан гар үйлдэл."
        ),
        default_weight=1.5,
        active_in_m1=False,
        why_deferred=(
            "wrist_to_torso-той ижил: holding=True зориулагдсан тул "
            "L4.5-ыг хүлээж байна."
        ),
    ),
]


class BehaviorListResponse(BaseModel):
    dimensions: list[BehaviorDimension]
    active_count: int
    total_count: int
    risk_thresholds: dict[str, float]
    # Risk_pct band map matches sentry-ai live_worker/behavior.py
    color_bands: dict[str, str]


@router.get("", response_model=BehaviorListResponse)
def list_behaviors() -> BehaviorListResponse:
    active = sum(1 for d in _DIMENSIONS if d.active_in_m1)
    return BehaviorListResponse(
        dimensions=_DIMENSIONS,
        active_count=active,
        total_count=len(_DIMENSIONS),
        risk_thresholds={"green_max": 30.0, "yellow_max": 70.0},
        color_bands={
            "green": "0 – 30%",
            "yellow": "30 – 70%",
            "red": "70 – 100%",
        },
    )
