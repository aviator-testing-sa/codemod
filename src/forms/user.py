from flask_wtf import FlaskForm
from wtforms import validators
from wtforms import BooleanField
from wtforms import TextAreaField
from wtforms import StringField
from wtforms import DecimalField
from wtforms import FileField


class EditBuyer(FlaskForm):
    slug = StringField(label="Username", validators=[validators.InputRequired()])
    email = StringField(label="Email", validators=[validators.InputRequired()])
    fullname = StringField(label="Full Name")
    image = FileField(label="Image")
    firm = BooleanField(label="Firm?")
    description = TextAreaField(label="Description")
    investments = DecimalField(label="Previous investment $")
    linkedin = StringField(label="LinkedIn", validators=[validators.Optional()])
    angellist = StringField(label="AngelList", validators=[validators.Optional()])
    twitter = StringField(label="Twitter", validators=[validators.Optional()])
    facebook = StringField(label="Facebook", validators=[validators.Optional()])



class EditSeller(FlaskForm):
    slug = StringField(label="Username", validators=[validators.InputRequired()])
    email = StringField(label="Email", validators=[validators.InputRequired()])
    fullname = StringField(label="Full Name")
    image = FileField(label="Image")
    firm = BooleanField(label="Firm?")
    description = TextAreaField(label="Description")
    linkedin = StringField(label="LinkedIn", validators=[validators.Optional()])
    angellist = StringField(label="AngelList", validators=[validators.Optional()])
    twitter = StringField(label="Twitter", validators=[validators.Optional()])
    facebook = StringField(label="Facebook", validators=[validators.Optional()])
