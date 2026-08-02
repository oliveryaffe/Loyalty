"""Shared FastAPI dependencies: DB session + JWT-authenticated merchant."""
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import Merchant
from app.services.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_current_merchant(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Merchant:
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

    merchant_id = payload.get("sub")
    if merchant_id is None:
        raise credentials_exception

    merchant = db.get(Merchant, merchant_id)
    if merchant is None:
        raise credentials_exception

    return merchant
