from flask_wtf import Form
from wtforms import PasswordField
from wtforms import TextField
from wtforms import RadioField
from wtforms.fields.html5 import EmailField

import common


class RegisterBuyer(Form):
    slug = common.SlugField()
    email = EmailField()
    fullname = TextField()
    password = PasswordField()
    is_firm = RadioField(choices=[('firm','Firm'),('individual', 'Individual')])
    investments = TextField()
    linkedin = TextField()
    angellist = TextField()
    twitter = TextField()
    facebook = TextField()

