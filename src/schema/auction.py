#
# auction.py
#
#
import datetime
from sqlalchemy import orm, ForeignKey
from sqlalchemy.orm import relationship, backref
from sqlalchemy import String, DateTime
from main import db
from base import Base
from sqlalchemy.orm import Mapped, mapped_column

# multiple auctions per listing

class Auction(Base):
    __tablename__ = 'auction'

    # who's doing the auction
    userid: Mapped[int] = mapped_column(ForeignKey('user.id'), nullable=False)

    status: Mapped[str] = mapped_column(String, default='started')             # started|expired|closed|accepted
    close_reason: Mapped[str] = mapped_column(String, nullable=True)       # sold_online, sold_outside, no_sale, closed_by_admin
    expiration: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=True)       # when it expires

    listingid: Mapped[int] = mapped_column(ForeignKey('listing.id', ondelete='CASCADE'), nullable=True)
    accepted_bid_id: Mapped[int] = mapped_column(ForeignKey('activity.id', ondelete='SET NULL'), nullable=True)

    listing: Mapped["Listing"] = relationship(backref=backref('auctions', uselist=True))
    user: Mapped["User"] = relationship()
    def has_expired(self):
        today = datetime.date.today()
        if self.expiration:
            return today > self.expiration.date()
        else:
            return False
