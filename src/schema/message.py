#
# message.py -- message table
#
#
from main import db
from base import Base
# Changed the import from 'sqlalchemy import orm' to 'sqlalchemy.orm' because in SQLAlchemy 2.x,
# the ORM functionality has been reorganized and accessing it directly from the orm module is the recommended approach
from sqlalchemy.orm import relationship

class Message(Base):
    __tablename__ = 'message'

    from_userid = db.Column(db.ForeignKey('user.id'), nullable=False)
    # Changed from 'orm.relationship' to 'relationship' to match the updated import
    from_user = relationship('User', primaryjoin='Message.from_userid == User.id')

    to_userid = db.Column(db.ForeignKey('user.id'), nullable=False)
    # Changed from 'orm.relationship' to 'relationship' to match the updated import
    to_user = relationship('User', primaryjoin='Message.to_userid == User.id')

    text = db.Column(db.Text)