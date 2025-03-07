#
# login.py - all your login db needs
#
#
from sqlalchemy import orm
from main import db
from base import Base


class Login(Base):
    __tablename__ = 'login'
    userid = db.Column(db.ForeignKey(u'user.id', ondelete=u'CASCADE'), nullable=False)
    date = db.Column(db.DateTime)
    # Changed relationship definition to use the new relationship API syntax in SQLAlchemy 2.x
    # The string-based name reference is replaced with a more explicit reference
    user = orm.relationship("User")