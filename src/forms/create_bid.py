from flask_wtf import FlaskForm
from wtforms import TextAreaField
from wtforms import TextField
from wtforms import validators

class CreateBidForm(FlaskForm):
    price = TextField(validators=[validators.InputRequired()])
    text = TextAreaField(validators=[validators.Optional()])
