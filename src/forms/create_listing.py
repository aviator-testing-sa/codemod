from datetime import date, timedelta
from common import get_select_choices
from config import categories
from flask_wtf import FlaskForm
from flask_wtf.file import FileField
from wtforms import validators
from wtforms import BooleanField
from wtforms import DateField
from wtforms import IntegerField
from wtforms import SelectField
from wtforms import TextAreaField
from wtforms import TextField
from wtforms.validators import Optional, InputRequired, DataRequired
from wtforms_components import DateRange


class CreateProductListing(FlaskForm):
    name = TextField(validators=[InputRequired()])
    domain = TextField(validators=[InputRequired()])
    category = SelectField(choices=get_select_choices(categories.ALL, placeholder="Choose Category"))
    app_ios = BooleanField()
    app_android = BooleanField()
    web_app = BooleanField()
    iot = BooleanField()
    robotics = BooleanField()
    cover_image = FileField()
    product_logo = FileField()


class CreateDetailListing(FlaskForm):
    linkedin = TextField()
    angellist = TextField()
    crunchbase = TextField()
    product_info = TextAreaField()
    tech_stack = TextAreaField()
    founder_info = TextAreaField()


class CreateBusinessListing(FlaskForm):
    incorporated = BooleanField()
    employees = IntegerField(validators=[Optional()])
    launch_date = DateField(format="%m/%d/%Y", validators=[
            Optional(),
            DateRange(max=date.today())])
    total_customers = IntegerField(validators=[Optional()])
    mau = IntegerField(validators=[Optional()])
    revenue = IntegerField(validators=[Optional()]) # convert from cents
    investment = IntegerField(validators=[Optional()]) # convert from cents
    misc_info = TextAreaField()

    #expiration = DateField(format="%m/%d/%Y", validators=[
    #        validators.Optional(),
    #        DateRange(min=date.today(), max=date.today() + timedelta(days=93))])
