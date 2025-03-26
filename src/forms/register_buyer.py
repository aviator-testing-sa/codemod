from flask_wtf import FlaskForm
from wtforms import PasswordField
from wtforms import StringField
from wtforms import RadioField
from wtforms.fields.html5 import EmailField

import common


class RegisterBuyer(FlaskForm):
    slug = common.SlugField()
    email = EmailField()
    fullname = StringField()
    password = PasswordField()
    is_firm = RadioField(choices=[('firm','Firm'),('individual', 'Individual')])
    investments = StringField()
    linkedin = StringField()
    angellist = StringField()
    twitter = StringField()
    facebook = StringField()