#
# activity.py - auctions have activities
#
#
from sqlalchemy import orm
from sqlalchemy.orm import relationship
from sqlalchemy import ForeignKey, String, Text, Integer, Index
from main import db
from base import Base


class Activity(Base):
    __tablename__ = 'activity'

    # auction this activity is for
    auctionid = db.Column(ForeignKey('auction.id'), nullable=False)
    auction = relationship('Auction', primaryjoin='Activity.auctionid == Auction.id')

    # user that created the activity
    userid = db.Column(ForeignKey('user.id'), nullable=False)
    user = relationship('User')

    # type of activity
    type = db.Column(String, index=True, nullable=False)   # bid|interest|auction
    status = db.Column(String)                             # new|cancel

    # info field
    text = db.Column(Text)

    # price (if it's a bid)
    price_cent = db.Column(Integer)

    #
    auction_type_idx = Index('auctionid', 'type')
