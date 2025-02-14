import datetime
from flask_wtf import Form
from wtforms import validators
from wtforms import BooleanField
from wtforms import DateField
from wtforms import TextAreaField
from wtforms import TextField


class EditListing(Form):
    name = TextField(validators=[validators.InputRequired()])
    domain = TextField(validators=[validators.InputRequired()])
    app_ios = BooleanField()
    app_android = BooleanField()
    incorporated = BooleanField()
    employees = TextField(validators=[validators.Optional()])

    product_info = TextAreaField()
    tech_stack = TextAreaField()
    founder_info = TextAreaField()

    launch_date = DateField(validators=[validators.Optional()])
    mau = TextField()
    revenue = TextField() # convert from cents
    investment = TextField() # convert from cents
    expiration = DateField(validators=[validators.Optional()])

    linkedin = TextField()
    angellist = TextField()
    crunchbase = TextField()

