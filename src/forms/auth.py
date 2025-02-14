#
# forms/auth.py
#
#
from flask_wtf import Form
from wtforms import PasswordField
from wtforms import TextField
from wtforms import RadioField
from wtforms import BooleanField
from wtforms.fields.html5 import EmailField
from wtforms import validators

import common


class Login(Form):
    name = TextField(label="Username or Email", validators=[validators.InputRequired()])
    password = PasswordField(label="Password", validators=[validators.InputRequired()])
    remember = BooleanField(label="Remember?", default="checked")


class Signup(Form):
    slug = common.SlugField(label="Username", validators=[validators.InputRequired()])
    email = EmailField(label="Email", validators=[validators.InputRequired()])
    password = PasswordField(label="Password", validators=[validators.InputRequired()])
    confirm = PasswordField(label="Confirm password", validators=[validators.EqualTo('password')])
    type = RadioField(label="Type", choices=[('buyer', 'Buyer'), ('seller', 'Seller')])


class ResetPasswordForgot(Form):
    password = PasswordField(label="New password", validators=[validators.InputRequired()])
    confirm = PasswordField(label="Confirm password", validators=[validators.EqualTo('password')])


class ResetPasswordNormal(Form):
    current = PasswordField(label="Current password", validators=[validators.InputRequired()])
    password = PasswordField(label="New password", validators=[validators.InputRequired()])
    confirm = PasswordField(label="Confirm password", validators=[validators.EqualTo('password')])
