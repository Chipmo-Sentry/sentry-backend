"""Alert business logic — level derivation."""

from sentry_backend.db.models.alert import AlertCategory, AlertLevel


def derive_alert_level(category: AlertCategory, confidence: float) -> AlertLevel:
    """Translate VLM verdict into actionable level.

    Tuning knob — adjust thresholds as we learn from real feedback.
    """
    # Browsing / low-confidence is NOT hidden: the node now surfaces every
    # sustained breach (with its clip) so the operator can review it. These land
    # at "log" (Бүртгэсэн) — visible in the menu but out of notify/review — so a
    # detected episode is never silently dropped, just deprioritised.
    if category == AlertCategory.browsing or confidence < 0.30:
        return AlertLevel.log
    if category == AlertCategory.other:
        return AlertLevel.log
    if category == AlertCategory.cart_pickup:
        return AlertLevel.log if confidence < 0.70 else AlertLevel.notify
    if category == AlertCategory.pocket_conceal:
        if confidence >= 0.85:
            return AlertLevel.review
        if confidence >= 0.70:
            return AlertLevel.notify
        return AlertLevel.log
    return AlertLevel.log
