"""Shared FastAPI dependencies: DB session + JWT-authenticated user/merchant.

`get_current_user` decodes the JWT and loads the authenticated `TeamMember`.
`get_current_merchant` is kept as a thin wrapper around it (returns
`current_user.merchant`) so existing routers (members.py, transactions.py,
rewards.py, ai.py) that depend on `get_current_merchant` need zero changes
for the multi-user-accounts feature.
"""
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import Merchant, TeamMember
from app.services.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> TeamMember:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise credentials_exception

    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception

    team_member_id = payload.get("sub")
    if team_member_id is None:
        raise credentials_exception

    user = db.get(TeamMember, team_member_id)
    if user is None or not user.is_active:
        raise credentials_exception

    return user


def get_current_merchant(current_user: TeamMember = Depends(get_current_user)) -> Merchant:
    return current_user.merchant


def require_admin(current_user: TeamMember = Depends(get_current_user)) -> TeamMember:
    if current_user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return current_user
