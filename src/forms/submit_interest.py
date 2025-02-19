from flask_wtf import FlaskForm
from wtforms import TextAreaField
from wtforms import validators

class SubmitInterestForm(FlaskForm):
    text = TextAreaField(validators=[validators.Optional()])
