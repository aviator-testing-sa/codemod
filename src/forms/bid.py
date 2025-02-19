from flask_wtf import FlaskForm
from wtforms import HiddenField
from wtforms import TextAreaField
from wtforms import TextField


class BidForm(FlaskForm):
    auction_id = HiddenField()
    price = TextField() # Convert to cents
    text = TextAreaField()
