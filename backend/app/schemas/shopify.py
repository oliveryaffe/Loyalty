"""Schemas for the (sandboxed) Shopify `orders/create` webhook payload.

Only a subset of Shopify's real Order resource field names is modeled here
-- the fields this app actually reads. `extra="ignore"` lets a real,
much-larger Shopify payload (hundreds of fields) validate fine.
"""
from pydantic import BaseModel, ConfigDict, EmailStr


class ShopifyCustomer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | str | None = None
    email: EmailStr
    first_name: str | None = None
    last_name: str | None = None


class ShopifyOrderWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | str
    order_number: int | str | None = None
    created_at: str | None = None
    total_price: str
    currency: str | None = None
    financial_status: str | None = None
    customer: ShopifyCustomer
    line_items: list[dict] = []
