# auction.py
#
#
import datetime
from sqlalchemy import orm
from main import db
from base import Base

# multiple auctions per listing

class Auction(Base):
    __tablename__ = 'auction'

    # who's doing the auction
    userid = db.Column(db.ForeignKey('user.id'), nullable=False)

    status = db.Column(db.String, default='started')             # started|expired|closed|accepted
    close_reason = db.Column(db.String)       # sold_online, sold_outside, no_sale, closed_by_admin
    expiration = db.Column(db.DateTime)       # when it expires

    listingid = db.Column(db.ForeignKey('listing.id', ondelete='CASCADE'), nullable=True)
    accepted_bid_id = db.Column(db.ForeignKey('activity.id', ondelete='SET NULL'), nullable=True)

    # Changed from using backref parameter to back_populates in relationship declarations
    # as backref is deprecated in favor of back_populates in SQLAlchemy 2.x
    listing = orm.relationship('Listing', back_populates='auctions')
    user = orm.relationship('User')

    def has_expired(self):
        today = datetime.date.today()
        return today > self.expiration.date()