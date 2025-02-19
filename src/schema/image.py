from sqlalchemy import orm
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import ForeignKey, Integer, String, Index

from main import db
from base import Base


class ListingImage(Base):
    __tablename__ = 'listing_image'

    listingid: Mapped[int] = mapped_column(ForeignKey('listing.id'))
    listing: Mapped["Listing"] = relationship(back_populates="images") # type: ignore

    sequence: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String, nullable=False)


    list_seq_idx = Index('listingid', 'sequence')
