#
# base.py - all tables should derive
#
#
import datetime

from sqlalchemy import Column, Integer, Boolean, DateTime
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    __abstract__ = True

    id = Column(Integer, primary_key=True)
    active = Column(Boolean, default=True)
    created = Column(DateTime, default=datetime.datetime.utcnow)
    updated = Column(DateTime, default=datetime.datetime.utcnow, onupdate=func.now())
