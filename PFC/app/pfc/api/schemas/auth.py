from typing import Literal

from pydantic import BaseModel, Field


class SetupAdmin(BaseModel):
    setup_code: str
    email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(max_length=1024)


class Login(BaseModel):
    role: Literal["Admin", "Analyst"]
    email: str
    password: str = Field(max_length=1024)


class ChangePassword(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class UserView(BaseModel):
    user_id: int
    email: str
    display_name: str
    role: str
    must_change_password: bool


class AuthView(BaseModel):
    user: UserView
    role_at_login: str
    must_change_password: bool
    csrf_token: str
