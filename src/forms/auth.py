from flask_wtf import FlaskForm
from wtforms import PasswordField
from wtforms import StringField
from wtforms import RadioField
from wtforms import BooleanField
from wtforms import EmailField
from wtforms import validators

import common


class Login(FlaskForm):
    name = StringField(label="Username or Email", validators=[validators.InputRequired()])
    password = PasswordField(label="Password", validators=[validators.InputRequired()])
    remember = BooleanField(label="Remember?", default="checked")


class Signup(FlaskForm):
    slug = common.SlugField(label="Username", validators=[validators.InputRequired()])
    email = EmailField(label="Email", validators=[validators.InputRequired()])
    password = PasswordField(label="Password", validators=[validators.InputRequired()])
    confirm = PasswordField(label="Confirm password", validators=[validators.EqualTo('password')])
    type = RadioField(label="Type", choices=[('buyer', 'Buyer'), ('seller', 'Seller')])


class ResetPasswordForgot(FlaskForm):
    password = PasswordField(label="New password", validators=[validators.InputRequired()])
    confirm = PasswordField(label="Confirm password", validators=[validators.EqualTo('password')])


class ResetPasswordNormal(FlaskForm):
    current = PasswordField(label="Current password", validators=[validators.InputRequired()])
    password = PasswordField(label="New password", validators=[validators.InputRequired()])
    confirm = PasswordField(label="Confirm password", validators=[validators.EqualTo('password')])
