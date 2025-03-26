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
from wtforms import StringField
from wtforms_components import DateRange


class CreateProductListing(FlaskForm):
    name = StringField(validators=[validators.InputRequired()])
    domain = StringField(validators=[validators.InputRequired()])
    category = SelectField(choices=get_select_choices(categories.ALL, placeholder="Choose Category"))
    app_ios = BooleanField()
    app_android = BooleanField()
    web_app = BooleanField()
    iot = BooleanField()
    robotics = BooleanField()
    cover_image = FileField()
    product_logo = FileField()


class CreateDetailListing(FlaskForm):
    linkedin = StringField()
    angellist = StringField()
    crunchbase = StringField()
    product_info = TextAreaField()
    tech_stack = TextAreaField()
    founder_info = TextAreaField()


class CreateBusinessListing(FlaskForm):
    incorporated = BooleanField()
    employees = IntegerField(validators=[validators.Optional()])
    launch_date = DateField(format="%m/%d/%Y", validators=[
            validators.Optional(),
            DateRange(max=date.today())])
    total_customers = IntegerField(validators=[validators.Optional()])
    mau = IntegerField(validators=[validators.Optional()])
    revenue = IntegerField(validators=[validators.Optional()]) # convert from cents
    investment = IntegerField(validators=[validators.Optional()]) # convert from cents
    misc_info = TextAreaField()

    #expiration = DateField(format="%m/%d/%Y", validators=[
    #        validators.Optional(),
    #        DateRange(min=date.today(), max=date.today() + timedelta(days=93))])