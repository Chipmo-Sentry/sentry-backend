"""L5 live alerts (camera.mediamtx_path, camera.risk_threshold, alert.triggered_by/person_id/peak_risk_pct)

Revision ID: d4f171ae7ba7
Revises: f7bd45a433a1
Create Date: 2026-05-28 17:40:53.707770

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f171ae7ba7'
down_revision: Union[str, None] = 'f7bd45a433a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create the enum type explicitly — Alembic autogen doesn't always do this
    alert_trigger = sa.Enum('manual_upload', 'live_threshold', name='alert_trigger')
    alert_trigger.create(op.get_bind(), checkfirst=True)

    op.add_column('alerts', sa.Column('triggered_by', alert_trigger, server_default='manual_upload', nullable=False))
    op.add_column('alerts', sa.Column('person_id', sa.Integer(), nullable=True))
    op.add_column('alerts', sa.Column('peak_risk_pct', sa.Float(), nullable=True))
    op.create_index(op.f('ix_alerts_triggered_by'), 'alerts', ['triggered_by'], unique=False)
    op.add_column('cameras', sa.Column('mediamtx_path', sa.String(length=64), nullable=True))
    # New non-null column with default 70.0 — backfill existing rows
    op.add_column('cameras', sa.Column('risk_threshold', sa.Float(), nullable=False, server_default='70.0'))
    op.create_index(op.f('ix_cameras_mediamtx_path'), 'cameras', ['mediamtx_path'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_cameras_mediamtx_path'), table_name='cameras')
    op.drop_column('cameras', 'risk_threshold')
    op.drop_column('cameras', 'mediamtx_path')
    op.drop_index(op.f('ix_alerts_triggered_by'), table_name='alerts')
    op.drop_column('alerts', 'peak_risk_pct')
    op.drop_column('alerts', 'person_id')
    op.drop_column('alerts', 'triggered_by')
    sa.Enum(name='alert_trigger').drop(op.get_bind(), checkfirst=True)
