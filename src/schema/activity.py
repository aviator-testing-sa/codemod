#
# activity.py - auctions have activities
#
#
from sqlalchemy import orm
from main import db
from base import Base


class Activity(Base):
    __tablename__ = 'activity'

    # auction this activity is for
    auctionid = db.Column(db.ForeignKey('auction.id'), nullable=False)
    auction = orm.relationship('Auction', primaryjoin='Activity.auctionid == Auction.id')

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
    auction_type_idx = db.Index('auctionid', 'type')
