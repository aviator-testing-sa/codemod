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
        return self.userid

    def get_name(self):
        return self.user.name

    def get_locale(self):
        return 'en_US'


class Anonymous:
    def __init__(self):
        self.authenticated = False
        self.active = False
        self.anonymous = True

    @property
    def is_authenticated(self):
        return False

    @property
    def is_active(self):
        return False

    @property
    def is_anonymous(self):
        return True

    def get_id(self):
        return None

    def get_locale(self):
        return 'en_US'