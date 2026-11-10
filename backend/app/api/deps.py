"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.core.settings import Settings
from app.db.session import get_session


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_app_config(request: Request) -> AppConfig:
    config: AppConfig = request.app.state.config
    return config


def get_engine(request: Request) -> Engine:
    engine: Engine = request.app.state.engine
    return engine


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ConfigDep = Annotated[AppConfig, Depends(get_app_config)]
EngineDep = Annotated[Engine, Depends(get_engine)]
SessionDep = Annotated[Session, Depends(get_session)]
