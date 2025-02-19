from flask_wtf import FlaskForm
from wtforms import TextAreaField
from wtforms import validators

class SendMessageForm(FlaskForm):
    text = TextAreaField(validators=[validators.InputRequired()])
