from flask_wtf import FlaskForm
from wtforms import PasswordField
from wtforms import validators
from wtforms.validators import Length, DataRequired, EqualTo


class ResetPasswordForm(FlaskForm):
    password = PasswordField('Password',
                             validators=[DataRequired(),
                                         Length(min=6, max=50)])

    password_confirm = PasswordField('Confirm Password',
                                     validators=[DataRequired(),
                                                 Length(min=6, max=50),
                                                 EqualTo('password', message='Passwords must match')])
