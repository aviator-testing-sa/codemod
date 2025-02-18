from sqlalchemy import orm
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.types import Integer


Base = declarative_base()

class Login(Base):
    __tablename__ = 'login'
    id: Mapped[int] = mapped_column(primary_key=True)
    userid: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    date: Mapped[DateTime] = mapped_column(DateTime)
    user: Mapped["User"] = relationship(back_populates="logins")  # Assuming User class exists
