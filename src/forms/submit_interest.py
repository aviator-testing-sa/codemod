from flask_wtf import Form
from wtforms import TextAreaField
from wtforms import validators

class SubmitInterestForm(Form):
    text = TextAreaField(validators=[validators.Optional()])
