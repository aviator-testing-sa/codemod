#
# user.py -- user table
#
#
import itertools
from main import db
from sqlalchemy import Column, String, Boolean, Integer
from sqlalchemy.orm import mapped_column, Mapped
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


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

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String, default=UserStatus.new)   # new|approved|banned

    # type
    type: Mapped[str] = mapped_column(String)           # buyer|seller|admin

    # unique name required; validation should ensure that this is treated in
    # a case insensitive way
    # AKA: username
    slug: Mapped[str] = mapped_column(String, unique=True, index=True)

    # non-unique display name
    fullname: Mapped[str] = mapped_column(String)

    # authentication(s)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True, unique=True)
    email_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)

    # username & password are now option
    password: Mapped[str] = mapped_column(String)
    salt: Mapped[str] = mapped_column(String)

    # info
    image: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)

    # firm
    firm: Mapped[bool] = mapped_column(Boolean)  # true if a firm

    # previous investments (in cents)
    investments: Mapped[int] = mapped_column(Integer)

    # authorization
    auth_linkedin: Mapped[str] = mapped_column(String)

    # links
    linkedin: Mapped[str] = mapped_column(String)
    angellist: Mapped[str] = mapped_column(String)
    facebook: Mapped[str] = mapped_column(String)
    twitter: Mapped[str] = mapped_column(String)

    # credit card
    stripe_customer_key: Mapped[str] = mapped_column(String)

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
        user = db.session.get(cls, int(user_id))
        return user if user.active else None

    @classmethod
    def get_by_slug(cls, slug):
        return db.session.execute(db.select(cls).filter_by(active=True, slug=slug)).scalar_one_or_none()

    @classmethod
    def get_by_email(cls, email):
        return db.session.execute(db.select(cls).filter_by(active=True, email=email)).scalar_one_or_none()

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
