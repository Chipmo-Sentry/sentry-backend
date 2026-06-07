"""Stores router — org-scoped CRUD."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.deps.db import get_db
from sentry_backend.deps.tenancy import (
    get_current_organization_id,
    get_current_organization_id_admin,
)
from sentry_backend.repository import store_repo
from sentry_backend.schemas.store import StoreCreate, StorePublic, StoreUpdate

router = APIRouter(prefix="/api/v1/stores", tags=["stores"])


@router.get("", response_model=list[StorePublic])
async def list_stores(
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> list[StorePublic]:
    stores = await store_repo.list_stores_for_org(db, org_id)
    return [StorePublic.model_validate(s) for s in stores]


@router.post("", response_model=StorePublic, status_code=status.HTTP_201_CREATED)
async def create_store(
    body: StoreCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> StorePublic:
    store = await store_repo.create_store(
        db,
        organization_id=org_id,
        name=body.name,
        address=body.address,
        timezone=body.timezone,
        telegram_chat_id=body.telegram_chat_id,
    )
    return StorePublic.model_validate(store)


@router.get("/{store_id}", response_model=StorePublic)
async def get_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id)],
) -> StorePublic:
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    return StorePublic.model_validate(store)


@router.patch("/{store_id}", response_model=StorePublic)
async def update_store(
    store_id: UUID,
    body: StoreUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> StorePublic:
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    store = await store_repo.update_store(
        db,
        store,
        name=body.name,
        address=body.address,
        timezone=body.timezone,
        telegram_chat_id=body.telegram_chat_id,
    )
    return StorePublic.model_validate(store)


@router.delete("/{store_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_store(
    store_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    org_id: Annotated[UUID, Depends(get_current_organization_id_admin)],
) -> None:
    store = await store_repo.get_store(db, store_id, org_id)
    if store is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    await store_repo.delete_store(db, store)
