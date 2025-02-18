#
# auction.py
#
#
import datetime
from sqlalchemy import orm, ForeignKey
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, String, DateTime
from main import db
from base import Base

# multiple auctions per listing

class Auction(Base):
    __tablename__ = 'auction'

    # who's doing the auction
    userid = Column(ForeignKey('user.id'), nullable=False)

    status = Column(String, default='started')             # started|expired|closed|accepted
    close_reason = Column(String)       # sold_online, sold_outside, no_sale, closed_by_admin
    expiration = Column(DateTime)       # when it expires

    listingid = Column(ForeignKey('listing.id', ondelete='CASCADE'), nullable=True)
    accepted_bid_id = Column(ForeignKey('activity.id', ondelete='SET NULL'), nullable=True)

    listing = relationship('Listing', backref=backref('auctions', uselist=True))
    user = relationship('User')
    def has_expired(self):
        today = datetime.date.today()
        return today > self.expiration.date()
