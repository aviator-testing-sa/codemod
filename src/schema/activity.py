#
# activity.py - auctions have activities
#
#
# Changed import to use orm directly from sqlalchemy instead of potentially outdated pattern
from sqlalchemy import orm, Index
from main import db
from base import Base


class Activity(Base):
    __tablename__ = 'activity'

    # auction this activity is for
    auctionid = db.Column(db.ForeignKey('auction.id'), nullable=False)
    # Updated relationship syntax to use back_populates instead of the older primaryjoin style
    auction = orm.relationship('Auction', back_populates='activities')

    # user that created the activity
    userid = db.Column(db.ForeignKey('user.id'), nullable=False)
    user = orm.relationship('User')

    # type of activity
    type = db.Column(db.String, index=True, nullable=False)   # bid|interest|auction
    status = db.Column(db.String)                             # new|cancel

    # info field
    text = db.Column(db.Text)

    # price (if it's a bid)
    price_cent = db.Column(db.Integer)

    #
    # Updated Index creation to use __table_args__ approach which is preferred in SQLAlchemy 2.x
    __table_args__ = (
        Index('auction_type_idx', 'auctionid', 'type'),
    )