import datetime
import errors
import sqlalchemy
from sqlalchemy import select, desc, func

from schema.activity import Activity
from schema.auction import Auction
from schema.listing import Listing
from schema.image import ListingImage
from main import db
from upload import uploader
import utils


def get_listing(listing_id):
    listing = db.session.get(Listing, int(listing_id))
    if listing is None:
        raise errors.NotFoundError("Listing not found")
    if listing.active:
        return listing
    raise errors.InvalidValueError("Cannot access listing")

def get_auction(auction_id):
    auction = db.session.get(Auction, int(auction_id))
    if auction is None:
        raise errors.NotFoundError("Auction not found")
    if auction.active:
        return auction
    raise errors.InvalidValueError("Cannot access auction")

def get_latest_auction(listing_id):
    stmt = select(Auction).where(Auction.listingid == int(listing_id), Auction.active == True)\
        .order_by(desc(Auction.id))
    auctions = db.session.execute(stmt).scalars().all()
    return auctions[0] if auctions else None


def create_product(user, form):
    slug = ensure_listing_slug(form.name.data)
    listing = Listing(userid=user.id)
    listing.status = 'draft'
    db.session.add(listing)
    listing.name = form.name.data
    listing.domain = form.domain.data
    listing.category = form.category.data
    listing.app_ios = form.app_ios.data
    listing.app_android = form.app_android.data
    listing.web_app = form.web_app.data
    listing.slug = slug
    if form.cover_image.data:
        listing.cover_image_url = uploader.s3_upload(form.cover_image.data)
    if form.product_logo.data:
        listing.product_icon_url = uploader.s3_upload(form.product_logo.data)
     # TODO: Handle screenshots
    db.session.commit()
    return listing


def create_details(listing, form):
    form.populate_obj(listing)
    db.session.commit()


def create_business(listing, form, status):
    form.populate_obj(listing)
    if form.revenue.data:
        listing.revenue_cents = int(form.revenue.data) * 100
    if form.investment.data:
        listing.investment_cents = int(form.investment.data) * 100
    if status == 'published':
        _start_auction(listing, form.expiration.data)

    db.session.commit()


def edit_listing(user, listing, form, status):
    if listing.userid != user.id:
      raise errors.InvalidRequestError("Cannot access the listing")

    if status not in ['draft', 'published']:
        raise errors.ValidationError("Invalid status")
    form.populate_obj(listing)
    listing.status = status
    listing.revenue_cents = int(form.revenue.data * 100)
    listing.investment_cents = int(form.investment.data * 100)
    if status == 'published':
        _start_auction(user, listing, form.expiration.data)
    db.session.commit()

def upload_file(user, listing, file):
    if listing.userid != user.id:
        raise errors.ValidationError("Cannot upload file")

    upload_url = uploader.s3_resize_and_upload(file)
    image = ListingImage(listingid=listing.id, url=upload_url)
    db.session.add(image)
    db.session.commit()



"""
This might actually just work with edit listing end point.
def publish_listing(user, listing):
    if listing.user_id != user.id:
        raise errors.InvalidRequestError("Cannot access the listing")
    if status not in ['draft', 'published']:
        raise errors.ValidationError("Invalid status")

    if listing.status == 'draft':
        listing.status = 'published'
        _start_auction(user, listing, form.expiration.data)
        d.session.commit()
    elif listing.status != 'published':
        raise errors.ValidationError("Cannot set the listing as published")
"""

def unpublish_listing(user, listing):
    # Find all related auctions and stop them
    if listing.user_id != user.id:
        raise errors.InvalidRequestError("Cannot access the listing")
    if listing.status == 'published':
        raise errors.InvalidRequestError("Cannot stop an unpublished listing")
    auctions = listing.auctions
    for auction in auctions:
        close_auction(user, auction)


def close_auction(user, auction):
    if not auction.active:
        raise errors.InvalidRequestError("Invalid listing")
    if auction.status != 'started':
        raise errors.InvalidRequestError("Cannot close inactive auction")
    auction.status = 'closed'
    auction.close_reason = 'TODO'
    activity = Activity(
        userid=user.id,
        type='auction',
        status='closed',
        auction=auction)
    db.session.add(activity)
    db.session.commit()
    #TODO: notify bidders and owners


def delete_listing(user, listing):
    listing.active = False
    for auction in listing.auctions:
        auction.active = False
    db.session.commit()
    # TODO: send notification to bidders and owner


def ensure_auction_active():
    # Update all expired auctions
    today = datetime.datetime.utcnow().date()
    stmt = select(Auction).where(Auction.active == True, Auction.status == 'started', Auction.expiration < today)
    auctions = db.session.execute(stmt).scalars().all()
    for auction in auctions:
        auction.status = 'expired'
    db.session.commit()
    # TODO: send notification to bidders and owners


def _start_auction(listing, expiration):
    auction =  Auction(userid=listing.userid,
        status='started',
        expiration=expiration)
    auction.listing = listing
    db.session.add(auction)

    # Also create an auction activity
    activity = Activity(
        userid=listing.userid,
        type='auction',
        status='new',
        auction=auction)
    db.session.add(activity)
    return auction

def send_message(from_user, form, to_user):
    pass


def ensure_listing_slug(name, current_slug=''):
    if not name:
        return current_slug or ''
    base_slug = utils.slugify(name)
    if current_slug == base_slug:
        return base_slug
    stmt = select(Listing).filter(func.lower(Listing.slug).like(base_slug + '%'))
    listings = db.session.execute(stmt).scalars().all()
    if listings:
        var = 1
        slugs = [listing.slug for listing in listings]
        while (base_slug + '-' + str(var)) in slugs:
            var += 1
        return base_slug + '-' + str(var)
    return base_slug


def build_search_listing(string, asmatch=False, order="-updated", limit=50, offset=0, **filters):
    """
    How to build a search query on listings
        string = search string (like query against name, category & domain)
        order = ("[-]field1", "field2")  where "-" is descending
        filters = {'field1' : ('[~]value1', [~]value2), ...}

    """
    cls = schema.listing.Listing

    def filter(qry, k, v):
        attr = getattr(cls, k)

        if not isinstance(v, (tuple, list)):
            if v.startswith("~"):
                return qry.filter(attr != v)
            return qry.filter(attr == v)

        if isinstance(v, str) and v.startswith("~"):
             return qry.filter(attr != v[1:])

        if isinstance(v, (tuple, list)):
            if isinstance(v[0], str) and v[0].startswith("~"):
                return qry.filter(~attr.in_([item[1:] if isinstance(item, str) and item.startswith("~") else item for item in v]))
            return qry.filter(attr.in_(v))
        return qry.filter(attr == v)

    # build query
    qry = select(cls)
    for k,v in filters.items():
        qry = filter(qry, k, v)
    # build search string
    if string is not None:
        f1 = cls.filter_like_name(string, asmatch=asmatch)
        f2 = cls.filter_like_category(string, asmatch=asmatch)
        f3 = cls.filter_like_domain(string, asmatch=asmatch)
        qry = qry.where(f1 | f2 | f3)

    # build ordering
    if order is not None:
        if isinstance(order, str):
            order = (order,)
        order_clauses = []
        for o in order:
            attr = getattr(cls, o.lstrip("-"))
            if o.startswith("-"):
                order_clauses.append(desc(attr))
            else:
                order_clauses.append(attr)
        if order_clauses:
            qry = qry.order_by(*order_clauses)

    # limit & offset
    stmt = qry
    if limit is not None:
        stmt = stmt.limit(limit)

    if offset > 0:
        stmt = stmt.offset(offset)

    return db.session.execute(stmt).scalars()
