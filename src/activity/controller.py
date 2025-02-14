import errors
from main import db
from schema.activity import Activity
from schema.auction import Auction


def create_bid(user, auction, form):
    if user.status != 'approved':
        raise errors.AccessDeniedError("You need to approve your account before bidding")
    if auction.status != 'started':
        raise errors.InvalidRequestError("Cannot bid for an inactive auction")
    price_cents = int(form.data.price) * 100
    activity = Activity(
        userid=user.id,
        type='bid',
        text=form.text.data,
        auctionid=auction.id,
        price_cent=price_cents)
    db.session.add(activity)
    db.session.commit()
    # notify seller


def submit_interest(user, auction, form):
    if user.status != 'approved':
        raise errors.AccessDeniedError("You need to approve your account before sending interest")
    if auction.status != 'started':
        raise errors.InvalidRequestError("Cannot submit interest for an inactive auction")
    activity = Activity(
        userid=user.id,
        type='interest',
        text=form.text.data,
        auctionid=auction.id)
    db.session.add(activity)
    db.session.commit()
    # notify seller


def get_activities(auction):
    return Activity.query.filter_by(auctionid=auction.id).filter_by(active=True).all()


def get_auction(auction_id):
    auction = Auction.query.get_or_404(int(auction_id))
    if auction.active:
        return auction
    raise errors.InvalidValueError("Cannot access auction")


def get_activity(activity_id):
    activity = Activity.query.get_or_404(int(activity_id))
    if activity.active:
        return activity
    raise errors.InvalidValueError("Cannot access activity")

