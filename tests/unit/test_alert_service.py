from sentry_backend.db.models.alert import AlertCategory, AlertLevel
from sentry_backend.services.alert_service import derive_alert_level


def test_benign_categories_are_ignored() -> None:
    assert derive_alert_level(AlertCategory.browsing, 0.99) == AlertLevel.ignore
    assert derive_alert_level(AlertCategory.cart_pickup, 0.99) == AlertLevel.ignore
    assert derive_alert_level(AlertCategory.other, 0.99) == AlertLevel.ignore


def test_concealment_thresholds_cover_attempts() -> None:
    assert derive_alert_level(AlertCategory.pocket_conceal, 0.49) == AlertLevel.ignore
    assert derive_alert_level(AlertCategory.pocket_conceal, 0.50) == AlertLevel.log
    assert derive_alert_level(AlertCategory.bag_conceal, 0.70) == AlertLevel.notify
    assert derive_alert_level(AlertCategory.bag_conceal, 0.85) == AlertLevel.review
