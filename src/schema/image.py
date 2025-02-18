#
# image.py - all your imaging needs
#
#
from sqlalchemy import orm, ForeignKey, String, Integer, Index
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Mapped, mapped_column


Base = declarative_base()


class ListingImage(Base):
    __tablename__ = 'listing_image'

    listingid: Mapped[int] = mapped_column(ForeignKey('listing.id'))
    listing: Mapped["Listing"] = relationship(back_populates="images")

    sequence: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String, nullable=False)


    list_seq_idx = Index('listingid', 'sequence')

