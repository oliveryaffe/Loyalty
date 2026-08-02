from pydantic import BaseModel, EmailStr


class MerchantSignup(BaseModel):
    business_name: str
    email: EmailStr
    password: str


class MerchantLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MerchantOut(BaseModel):
    id: str
    business_name: str
    email: EmailStr

    class Config:
        from_attributes = True
