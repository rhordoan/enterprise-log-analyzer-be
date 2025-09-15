from pydantic import BaseModel, Field


class ItemBase(BaseModel):
    title: str = Field(..., max_length=100)
    description: str | None = None


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=100)
    description: str | None = None


class ItemInDBBase(ItemBase):
    id: int

    class Config:
        orm_mode = True


class Item(ItemInDBBase):
    """Additional fields for public response if needed."""

    pass
