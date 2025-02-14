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
    user = orm.relationship(u'User')

