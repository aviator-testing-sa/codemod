#
# base.py - all tables should derive
#
#
import datetime

from sqlalchemy import Column, Integer, Boolean, DateTime
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped, mapped_column


class Base(DeclarativeBase):
    __abstract__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow)
