from flask_wtf import Form
from wtforms import HiddenField
from wtforms import TextAreaField
from wtforms import TextField


class BidForm(Form):
    auction_id = HiddenField()
    price = TextField() # Convert to cents
    text = TextAreaField()

