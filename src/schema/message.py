#
# message.py -- message table
#
#
from main import db
from base import Base
from sqlalchemy import orm

class Message(Base):
    __tablename__ = 'message'

    from_userid = db.Column(db.ForeignKey('user.id'), nullable=False)
    from_user = orm.relationship('User', primaryjoin='Message.from_userid == User.id')

    to_userid = db.Column(db.ForeignKey('user.id'), nullable=False)
    to_user = orm.relationship('User', primaryjoin='Message.to_userid == User.id')

    text = db.Column(db.Text)
