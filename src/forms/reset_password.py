from flask_wtf import FlaskForm
from wtforms import PasswordField
from wtforms import validators


class ResetPasswordForm(FlaskForm):
    password = PasswordField('',
            validators=[validators.DataRequired(),
                        validators.Length(min=6, max=50)])

    password_confirm = PasswordField('',
            validators=[validators.DataRequired(),
                        validators.Length(min=6, max=50)])

    def validate(self, forgot=False):
        if not FlaskForm.validate(self):
            return False
        if self.password.data != self.password_confirm.data:
            self.password_confirm.errors.append('Passwords do not match.')
            return False
        return True