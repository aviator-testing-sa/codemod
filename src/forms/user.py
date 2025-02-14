from flask_wtf import Form
from wtforms import validators
from wtforms import BooleanField
from wtforms import TextAreaField
from wtforms import TextField
from wtforms import DecimalField
from wtforms import FileField


class EditBuyer(Form):
    slug = TextField(label="Username", validators=[validators.InputRequired()])
    email = TextField(label="Email", validators=[validators.InputRequired()])
    fullname = TextField(label="Full Name")
    image = FileField(label="Image")
    firm = BooleanField(label="Firm?")
    description = TextAreaField(label="Description")
    investments = DecimalField(label="Previous investment $")
    linkedin = TextField(label="LinkedIn", validators=[validators.Optional()])
    angellist = TextField(label="AngelList", validators=[validators.Optional()])
    twitter = TextField(label="Twitter", validators=[validators.Optional()])
    facebook = TextField(label="Facebook", validators=[validators.Optional()])



class EditSeller(Form):
    slug = TextField(label="Username", validators=[validators.InputRequired()])
    email = TextField(label="Email", validators=[validators.InputRequired()])
    fullname = TextField(label="Full Name")
    image = FileField(label="Image")
    firm = BooleanField(label="Firm?")
    description = TextAreaField(label="Description")
    linkedin = TextField(label="LinkedIn", validators=[validators.Optional()])
    angellist = TextField(label="AngelList", validators=[validators.Optional()])
    twitter = TextField(label="Twitter", validators=[validators.Optional()])
    facebook = TextField(label="Facebook", validators=[validators.Optional()])

