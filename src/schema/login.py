from sqlalchemy import orm
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey
from sqlalchemy import DateTime
from main import db
from base import Base


class Login(Base):
    __tablename__ = 'login'
    userid: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    date: Mapped[DateTime] = mapped_column(DateTime)
    user: Mapped["User"] = relationship(back_populates="logins")  # Assuming User class has 'logins' relationship
