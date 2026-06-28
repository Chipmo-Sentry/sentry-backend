"""Alert business logic — level derivation."""

from sentry_backend.db.models.alert import AlertCategory, AlertLevel


def derive_alert_level(category: AlertCategory, confidence: float) -> AlertLevel:
    """Translate VLM verdict into actionable level.

    Evidence clips should only be retained for theft or attempted theft:
    concealment into clothing/pockets or bags. Visible shopping actions are
    ignored so they do not create saved evidence clips.
    """
    if category in (AlertCategory.browsing, AlertCategory.cart_pickup, AlertCategory.other):
        return AlertLevel.ignore

    if category in (AlertCategory.pocket_conceal, AlertCategory.bag_conceal):
        if confidence < 0.50:
            return AlertLevel.ignore
        if confidence >= 0.85:
            return AlertLevel.review
        if confidence >= 0.70:
            return AlertLevel.notify
        return AlertLevel.log

    return AlertLevel.ignore
