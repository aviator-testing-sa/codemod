#
# user.py -- user table
#
#
import itertools
from main import db
from base import Base


class UserStatus(object):
    new = 'new'
    approved = 'approved'
    banned = 'banned'



class UserType(object):
    buyer = 'buyer'
    seller = 'seller'
    admin = 'admin'


# FIXME: move to someplace generic
class EncodeMode(object):
    public = 'public'
    private = 'private'
    mine = 'mine'


class User(Base):
    __tablename__ = 'user'

    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    status = db.Column(db.String(50), default=UserStatus.new)   # new|approved|banned

    # type
    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    type = db.Column(db.String(50))           # buyer|seller|admin

    # unique name required; validation should ensure that this is treated in
    # a case insensitive way
    # AKA: username
    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    slug = db.Column(db.String(255), unique=True, index=True)

    # non-unique display name
    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    fullname = db.Column(db.String(255))

    # authentication(s)
    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    # Changed default=False to nullable=False since False is not a valid value for a String column
    email = db.Column(db.String(255), nullable=False, index=True, unique=True)
    email_confirmed = db.Column(db.Boolean, default=False)

    # username & password are now option
    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    password = db.Column(db.String(255))
    salt = db.Column(db.String(255))

    # info
    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    image = db.Column(db.String(255))
    description = db.Column(db.String(1024))

    # firm
    firm = db.Column(db.Boolean)  # true if a firm

    # previous investments (in cents)
    investments = db.Column(db.Integer)

    # authorization
    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    auth_linkedin = db.Column(db.String(255))

    # links
    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    linkedin = db.Column(db.String(255))
    angellist = db.Column(db.String(255))
    facebook = db.Column(db.String(255))
    twitter = db.Column(db.String(255))

    # credit card
    # Changed from db.String to db.String(length) to be explicit about the length as required in SQLAlchemy 2.1
    stripe_customer_key = db.Column(db.String(255))

    @property
    def display_name(self):
        return self.fullname or self.slug

    @property
    def profile_image(self):
        if self.image:
            return self.image
        return '/static/images/profile.png'

    @classmethod
    def get(cls, user_id):
        # Updated to use session.get() instead of Query.get() which is deprecated in SQLAlchemy 2.x
        user = db.session.get(User, int(user_id))
        return user if user and user.active else None

    @classmethod
    def get_by_slug(cls, slug):
        # Updated query syntax to use select() and scalar_one_or_none() pattern for SQLAlchemy 2.x
        from sqlalchemy import select
        stmt = select(User).where(User.active == True, User.slug == slug)
        return db.session.scalars(stmt).first()

    @classmethod
    def get_by_email(cls, email):
        # Updated query syntax to use select() and scalar_one_or_none() pattern for SQLAlchemy 2.x
        from sqlalchemy import select
        stmt = select(User).where(User.active == True, User.email == email)
        return db.session.scalars(stmt).first()

    @classmethod
    def validate_slug(cls, slug):
        user = cls.get_by_slug(slug)
        if user is None:
            return slug

        for n in itertools.count(1):
            s2 = slug + " %s" % n
            user = cls.get_by_slug(s2)
            if user is None:
                break

        return s2

    # Fixed missing self parameter in method definition
    def is_approved(self):
        return self.status == UserStatus.approved

    def is_buyer(self):
        return self.type == UserType.buyer

    def is_seller(self):
        return self.type == UserType.seller

    def is_admin(self):
        return self.type == UserType.admin

    def encode(self, mode=EncodeMode.public):
        """
        Encode user data; there are three modes: public, private & mine
            'public' means data is available to be seen by anyone
            'private' means only approved users on site can see data
            'mine' means only owner can see data
        """
        data = {
            'id' : self.id,
            'slug' : self.slug,
            'type' : self.type,
            'fullname' : self.fullname,
            'firm' : self.firm,
            'linkedin' : self.linkedin,
            'angellist' : self.angellist,
            'facebook' : self.facebook,
            'twitter' : self.twitter,
        }

        if mode == EncodeMode.public:
            return data

        data['investments'] = self.investments
        data['email'] = self.email

        if mode == EncodeMode.private:
            return data

        data['status'] = self.status

        return data
