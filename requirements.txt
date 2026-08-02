from pydantic import BaseModel


class RewardCreate(BaseModel):
    name: str
    description: str = ""
    category: str = "general"
    points_cost: int
    tier_required: str = "bronze"


class RewardOut(BaseModel):
    id: str
    name: str
    description: str
    category: str
    points_cost: int
    tier_required: str
    active: bool

    class Config:
        from_attributes = True


class RedemptionRequest(BaseModel):
    member_id: str
    reward_id: str


class RedemptionOut(BaseModel):
    id: str
    member_id: str
    reward_id: str
    points_spent: int
    status: str

    class Config:
        from_attributes = True
