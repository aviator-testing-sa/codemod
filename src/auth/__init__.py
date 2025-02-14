import utils
from flask.ext.login import current_user
from functools import wraps


def buyer_required(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if current_user.is_authenticated and \
            (current_user.user.is_buyer() or current_user.user.is_admin()):
            return func(*args, **kwargs)
        else:
            return utils.abort(400, detail="Not an authorized buyer")
    return wrapped


def seller_required(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if current_user.is_authenticated and \
            (current_user.user.is_seller() or current_user.user.is_admin()):
            return func(*args, **kwargs)
        else:
            print "ABORTING@@"
            return utils.abort(400, detail="Not an authorized seller")
    return wrapped

