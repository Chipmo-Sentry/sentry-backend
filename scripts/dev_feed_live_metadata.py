"""Dev-only: feed synthetic live metadata (REV.2 fields) into the local backend.

Verifies the full wire path without cameras/AI node: POST /internal/live-metadata
→ LiveTrack validation → broker → /ws/live/{cam} → frontend overlay + panel.

    uv run python scripts/dev_feed_live_metadata.py [seconds]
"""

from __future__ import annotations

import math
import sys
import time

import httpx

BACKEND = "http://localhost:8000"
TOKEN = "dev-service-token"  # .env LIVE_METADATA_SHARED_SECRET
CAM = "cam1_hik"


def frame(now_ms: int, t: float) -> dict:
    # Person 1 drifts slowly so the overlay visibly tracks; episode opened 45s ago.
    x = 600 + 180 * math.sin(t / 3)
    return {
        "camera_id": CAM,
        "frame_id": int(t * 5),
        "ts_ms": now_ms,
        "width": 1920,
        "height": 1080,
        "fps_inference": 5.0,
        "tracks": [
            {
                "person_id": 1,
                "box": [x, 260, x + 320, 980],
                "det_confidence": 0.91,
                "risk_pct": 62.0,
                "color": "red",
                "level": "HIGH",
                "state": "CONCEALMENT",
                "sequences": ["seq_pickup_wrist"],
                "behaviors": [
                    "looking_around",
                    "item_pickup",
                    "wrist_to_torso",
                    "seq_pickup_wrist",
                ],
                "behavior_scores": {
                    "looking_around": 6.0,
                    "item_pickup": 10.0,
                    "wrist_to_torso": 36.0,
                    "seq_pickup_wrist": 10.0,
                },
                "reasons": ["Хувцас доор нуух (24f)", "Орчноо харах"],
                "episode_started_ms": now_ms - 45_000,
                "store_person_id": 7,
                "store_risk_pct": 65.5,
            },
            {
                "person_id": 2,
                "box": [1300, 300, 1520, 900],
                "det_confidence": 0.84,
                "risk_pct": 4.0,
                "color": "green",
                "level": "LOW",
                "state": "IDLE",
            },
        ],
    }


def main() -> None:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 240.0
    headers = {"Authorization": f"Bearer {TOKEN}"}
    t0 = time.time()
    sent = 0
    with httpx.Client(timeout=5.0) as client:
        while time.time() - t0 < duration:
            t = time.time() - t0
            body = {"frames": [frame(int(time.time() * 1000), t)]}
            r = client.post(f"{BACKEND}/api/v1/internal/live-metadata", json=body, headers=headers)
            if r.status_code != 202:
                print("FAIL", r.status_code, r.text[:200])
                return
            sent += 1
            if sent % 25 == 0:
                print(f"sent={sent}")
            time.sleep(0.2)
    print(f"done sent={sent}")


if __name__ == "__main__":
    main()
