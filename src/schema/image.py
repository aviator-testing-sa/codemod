#
# image.py - all your imaging needs
#
#
from sqlalchemy import orm
from main import db
from base import Base


class ListingImage(Base):
    __tablename__ = 'listing_image'

    listingid = db.Column(db.ForeignKey('listing.id'))
    listing = orm.relationship('Listing', backref=orm.backref('images', uselist=True))

    sequence = db.Column(db.Integer)
    url = db.Column(db.String, nullable=False)


    list_seq_idx = db.Index('listingid', 'sequence')
