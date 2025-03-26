#
# listing.py
#
#
from sqlalchemy import orm
from main import db
from base import Base


class Listing(Base):
    __tablename__ = 'listing'

    # who's doing the listing
    userid = db.Column(db.ForeignKey('user.id'), nullable=False)

    #
    status = db.Column(db.String)     # draft|published

    # info
    name = db.Column(db.String, nullable=False, index=True)
    domain = db.Column(db.String, nullable=False, index=True)
    category = db.Column(db.String, nullable=False, index=True)
    slug = db.Column(db.String, nullable=False, index=True) # autogenerated

    app_ios = db.Column(db.Boolean, default=False)
    app_android = db.Column(db.Boolean, default=False)
    web_app = db.Column(db.Boolean, default=False)
    iot = db.Column(db.Boolean, default=False)
    robotics = db.Column(db.Boolean, default=False)

    # images
    cover_image_url = db.Column(db.String)
    product_icon_url = db.Column(db.String)
    # other images are stored as ListingImage in image schema

    incorporated = db.Column(db.Boolean, default=False)
    employees = db.Column(db.Integer)

    product_info = db.Column(db.Text)
    tech_stack = db.Column(db.Text)
    founder_info = db.Column(db.Text)

    # busienss metrics
    launch_date = db.Column(db.DateTime)      # day it was launched
    mau = db.Column(db.Integer)               # monthly active users
    revenue_cents = db.Column(db.Integer)     # monthly revenue in cents
    investment_cents = db.Column(db.Integer)  # total investment
    total_customers = db.Column(db.Integer)   # size of customer base
    misc_info = db.Column(db.Text)            # additional info

    # links
    linkedin = db.Column(db.String)
    angellist = db.Column(db.String)
    crunchbase = db.Column(db.String)

    user = orm.relationship('User', backref=orm.backref('listings', uselist=True))

    @property
    def cover_image(self):
        return self.cover_image_url or '/static/images/bg.jpg'

    @property
    def product_icon(self):
        return self.product_icon_url or '/static/images/avatar.jpg'

    @classmethod
    def filter_like_name(cls, name, asmatch=False):
        if asmatch:
            return cls.name.match(name)
        return cls.name.like('%{}%'.format(name))

    @classmethod
    def filter_like_domain(cls, domain, asmatch=False):
        if asmatch:
            return cls.domain.match(domain)
        return cls.domain.like('%{}%'.format(domain))

    @classmethod
    def filter_like_category(cls, category, asmatch=False):
        if asmatch:
            return cls.category.match(category)
        return cls.category.like('%{}%'.format(category))