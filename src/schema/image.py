#
# image.py - all your imaging needs
#
#
from sqlalchemy import orm, Index
from main import db
from base import Base


class ListingImage(Base):
    __tablename__ = 'listing_image'

    listingid = db.Column(db.ForeignKey('listing.id'))
    # Updated relationship definition to use relationship options directly rather than backref
    # In SQLAlchemy 2.x, while backref still works, it's recommended to use back_populates
    listing = orm.relationship('Listing', back_populates='images')

    sequence = db.Column(db.Integer)
    url = db.Column(db.String, nullable=False)

    # Updated index definition to use __table_args__ with Index directly
    # SQLAlchemy 2.x recommends using __table_args__ for indices and constraints
    __table_args__ = (
        Index('list_seq_idx', 'listingid', 'sequence'),
    )