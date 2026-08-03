from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr


class MerchantSignup(BaseModel):
    """Creates a Merchant + its first TeamMember (role=admin) under the hood."""

    business_name: str
    email: EmailStr
    password: str


class MerchantLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeOut(BaseModel):
    """Response shape for /auth/signup and /auth/me.

    Keeps top-level `business_name`/`email` fields (rather than nesting
    under `merchant`) so the existing frontend, which reads
    `merchant.business_name`/`merchant.email` off this object, keeps
    working with zero frontend changes. `id` is now the TeamMember id, not
    the Merchant id -- documented as a deliberate, safe rename (confirmed
    via grep the frontend never reads `.id` off this object today).
    """

    id: str
    merchant_id: str
    business_name: str
    email: EmailStr
    role: str

    class Config:
        from_attributes = True


class TeamMemberCreate(BaseModel):
    email: EmailStr
    password: str
    role: Literal["admin", "member"] = "member"


class TeamMemberOut(BaseModel):
    id: str
    email: EmailStr
    role: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class RoleUpdate(BaseModel):
    role: Literal["admin", "member"]
