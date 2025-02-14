from flask_wtf import Form
from wtforms import TextAreaField
from wtforms import validators

class SendMessageForm(Form):
    text = TextAreaField(validators=[validators.InputRequired()])
