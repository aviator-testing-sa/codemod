from flask_login import AnonymousUserMixin


class CurrentUser:
    def __init__(self, user):
        self.userid = user.id
        self.authenticated = True
        self.active = user.active
        self.anonymous = False
        self.user = user
        self.linkedin = None

    @property
    def is_authenticated(self):
        return self.authenticated

    @property
    def is_active(self):
        return self.active

    @property
    def is_anonymous(self):
        return self.anonymous

    def get_id(self):
        return str(self.userid)  # Flask-Login expects a string

    def get_name(self):
        return self.user.name

    def get_locale(self):
        return 'en_US'


class Anonymous(AnonymousUserMixin):
    def get_locale(self):
        return 'en_US'
