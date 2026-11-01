"""App profiles: list them (everyone), and configure them (platform administrators)."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import app_profiles
from ..auth import User, require_admin
from .. import settings_access
from ..db import get_db
from ..deps import get_actor
from ..models import AppGroup, AppProfile
from .common import audit

router = APIRouter(tags=["app profiles"])


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    features: List[str] = Field(default_factory=list)
    default: bool = Field(default=False, description="The profile of groups without one; exactly one profile is the default")


def profile_out(db, p: AppProfile) -> dict:
    groups = sorted(g.name for g in db.scalars(select(AppGroup).where(AppGroup.profile_id == p.id)))
    if p.default:
        groups += sorted(g.name for g in db.scalars(select(AppGroup).where(AppGroup.profile_id.is_(None))))
    return {"id": p.id, "name": p.name, "description": p.description, "features": p.features, "default": p.default,
            "groups": groups}


@router.get("/v1/app-profiles")
def list_profiles(db: Session = Depends(get_db), _: User = Depends(get_actor)):
    """Every profile with its features and groups, and the features there are."""
    return {"profiles": [profile_out(db, p) for p in db.scalars(select(AppProfile).order_by(AppProfile.name))],
            "features": [{"key": k, "label": v} for k, v in app_profiles.FEATURES.items()]}


@router.put("/v1/admin/app-profiles/{name}")
def put_profile(name: str, body: ProfileIn, db: Session = Depends(get_db), user: User = Depends(settings_access.needs("set-profiles", "manage"))):
    """Create or change a profile by name (a different body name renames it)."""
    profile = db.scalar(select(AppProfile).where(AppProfile.name == name))
    if body.name != name and db.scalar(select(AppProfile).where(AppProfile.name == body.name)):
        raise HTTPException(409, f"an app profile named {body.name} already exists")
    if profile is None:
        profile = AppProfile(name=body.name)
        db.add(profile)
    before = list(profile.features or [])
    profile.name, profile.description, profile.features = body.name, body.description, app_profiles.normalize(body.features)
    if body.default and not profile.default:
        db.execute(update(AppProfile).values(default=False))
    profile.default = body.default
    db.flush()
    audit(db, user.username, "app_profile.put", profile.id, name=profile.name, before=before, features=profile.features,
          default=profile.default)
    db.commit()
    return profile_out(db, profile)


@router.delete("/v1/admin/app-profiles/{name}", status_code=204)
def delete_profile(name: str, db: Session = Depends(get_db), user: User = Depends(settings_access.needs("set-profiles", "manage"))):
    profile = db.scalar(select(AppProfile).where(AppProfile.name == name))
    if profile is None:
        raise HTTPException(404, "app profile not found")
    used = sorted(g.name for g in db.scalars(select(AppGroup).where(AppGroup.profile_id == profile.id)))
    if used:
        raise HTTPException(409, "still given to: " + ", ".join(used))
    audit(db, user.username, "app_profile.delete", profile.id, name=profile.name)
    db.delete(profile)
    db.commit()
