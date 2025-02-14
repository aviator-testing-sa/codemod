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

    status = db.Column(db.String, default=UserStatus.new)   # new|approved|banned

    # type
    type = db.Column(db.String)           # buyer|seller|admin

    # unique name required; validation should ensure that this is treated in
    # a case insensitive way
    # AKA: username
    slug = db.Column(db.String, unique=True, index=True)

    # non-unique display name
    fullname = db.Column(db.String)

    # authentication(s)
    email = db.Column(db.String, nullable=False, index=True, unique=True, default=False)
    email_confirmed = db.Column(db.Boolean, default=False)

    # username & password are now option
    password = db.Column(db.String)
    salt = db.Column(db.String)

    # info
    image = db.Column(db.String)
    description = db.Column(db.String)

    # firm
    firm = db.Column(db.Boolean)  # true if a firm

    # previous investments (in cents)
    investments = db.Column(db.Integer)

    # authorization
    auth_linkedin = db.Column(db.String)

    # links
    linkedin = db.Column(db.String)
    angellist = db.Column(db.String)
    facebook = db.Column(db.String)
    twitter = db.Column(db.String)

    # credit card
    stripe_customer_key = db.Column(db.String)

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
        user = User.query.get(int(user_id))
        return user if user.active else None

    @classmethod
    def get_by_slug(cls, slug):
        return User.query.filter_by(active=True).filter_by(slug=slug).first()

    @classmethod
    def get_by_email(cls, email):
        return User.query.filter_by(active=True).filter_by(email=email).first()

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

    def is_approved():
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


