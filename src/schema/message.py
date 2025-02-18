#
# message.py -- message table
#
#
from main import db
from base import Base
from sqlalchemy import orm
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey, Text


class Message(Base):
    __tablename__ = 'message'

    from_userid: Mapped[int] = mapped_column(ForeignKey('user.id'))
    from_user = relationship('User', primaryjoin='Message.from_userid == User.id', foreign_keys=[from_userid])

    to_userid: Mapped[int] = mapped_column(ForeignKey('user.id'))
    to_user = relationship('User', primaryjoin='Message.to_userid == User.id', foreign_keys=[to_userid])

    text: Mapped[str] = mapped_column(Text)
