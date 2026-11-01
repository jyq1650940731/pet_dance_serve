import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.domain.constants import MAX_IMAGE_BYTES
from app.domain.phones import normalize_phone

Category = Literal["recommend", "dance", "comedy", "daily"]
PetScope = Literal["dog", "cat", "any"]
PetType = Literal["dog", "cat", "other"]
TemplateStatus = Literal["draft", "published", "unpublished"]


class ConsentIn(BaseModel):
    terms_version: str = Field(min_length=1, max_length=32)
    privacy_version: str = Field(min_length=1, max_length=32)


class SmsIn(BaseModel):
    phone: str

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return normalize_phone(value)


class LoginIn(SmsIn):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    pet_type: PetType | None = None
    consent: ConsentIn


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class LogoutIn(RefreshIn):
    pass


class CreditOut(BaseModel):
    balance: int
    frozen: int


class UserOut(BaseModel):
    id: uuid.UUID
    phone_masked: str
    pet_type: str | None
    status: str
    credits: CreditOut
    successful_count: int


class SmsOut(BaseModel):
    cooldown_sec: int


class AppTokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    gifted: bool
    user: UserOut


class RefreshOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class MePatch(BaseModel):
    pet_type: PetType | None


class MetaOut(BaseModel):
    maintenance: bool
    maintenance_message: str
    terms_version: str
    privacy_version: str
    cheapest_credit_cost: int | None
    gift_credits: int


class TemplateOut(BaseModel):
    id: uuid.UUID
    title: str
    subtitle: str
    category: str
    cover_url: str | None
    preview_url: str | None
    duration_sec: int
    aspect_ratio: str
    credit_cost: int
    pet_scope: str
    sort: int


class TemplateListOut(BaseModel):
    items: list[TemplateOut]
    page: int
    page_size: int
    total: int
    featured_id: uuid.UUID | None


class ImageUploadIn(BaseModel):
    content_type: Literal["image/jpeg"]
    content_length: int = Field(gt=0, le=MAX_IMAGE_BYTES)


class ImageUploadOut(BaseModel):
    image_key: str
    upload_url: str
    headers: dict[str, str]
    expires_in: int


class GenerationIn(BaseModel):
    template_id: uuid.UUID
    image_key: str = Field(min_length=1, max_length=240)
    ownership_confirmed: bool


class GenerationOut(BaseModel):
    id: uuid.UUID
    status: str
    stage_label: str
    template_id: uuid.UUID
    template_title: str
    duration_sec: int
    credit_cost: int
    error_code: str | None
    error_message: str | None
    playable: bool
    video_url: str | None
    cover_url: str | None
    created_at: datetime
    finished_at: datetime | None


class GenerationListOut(BaseModel):
    items: list[GenerationOut]
    page: int
    page_size: int
    total: int


class StaffLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class StaffTokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    username: str


class AdminUploadIn(BaseModel):
    kind: Literal["cover", "preview", "audio"]
    content_type: str
    content_length: int = Field(gt=0)


class AdminUploadOut(BaseModel):
    key: str
    upload_url: str
    headers: dict[str, str]
    expires_in: int


class TemplateWrite(BaseModel):
    title: str = Field(min_length=1, max_length=40)
    subtitle: str = Field(default="", max_length=200)
    category: Category
    duration_sec: Literal[5, 10] = 5
    credit_cost: int = Field(default=10, ge=1, le=10000)
    pet_scope: PetScope
    provider: str = Field(default="kling", max_length=32)
    model: str = Field(default="kling-v2.5-turbo", max_length=64)
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str = Field(min_length=1, max_length=2000)
    audio_asset_id: uuid.UUID | None = None
    cover_key: str | None = None
    preview_key: str | None = None
    sort: int = 0
    status: TemplateStatus = "draft"


class TemplatePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=40)
    subtitle: str | None = Field(default=None, max_length=200)
    category: Category | None = None
    duration_sec: Literal[5, 10] | None = None
    credit_cost: int | None = Field(default=None, ge=1, le=10000)
    pet_scope: PetScope | None = None
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=64)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    negative_prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    audio_asset_id: uuid.UUID | None = None
    cover_key: str | None = None
    preview_key: str | None = None
    sort: int | None = None
    status: TemplateStatus | None = None


class AdminTemplateOut(BaseModel):
    id: uuid.UUID
    title: str
    subtitle: str
    category: str
    status: str
    sort: int
    duration_sec: int
    aspect_ratio: str
    credit_cost: int
    pet_scope: str
    provider: str
    model: str
    prompt: str
    negative_prompt: str
    audio_asset_id: uuid.UUID | None
    cover_key: str | None
    preview_key: str | None
    cover_url: str | None
    preview_url: str | None
    today_count: int
    created_at: datetime
    updated_at: datetime


class AdminTemplateListOut(BaseModel):
    items: list[AdminTemplateOut]
    page: int
    page_size: int
    total: int
