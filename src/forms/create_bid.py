from flask_wtf import Form
from wtforms import TextAreaField
from wtforms import TextField
from wtforms import validators

class CreateBidForm(Form):
    price = TextField(validators=[validators.InputRequired()])
    text = TextAreaField(validators=[validators.Optional()])
