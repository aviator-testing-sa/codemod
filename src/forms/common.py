#
# forms/common.py
#
#
import decimal
import urllib.parse
import wtforms


def validate_unquoted(form, field):
    """
    True if quoted field is same as unquoted
    """
    return urllib.parse.quote(field.data) == field.data


def filter_lowercase(string):
    """
    Ensure string is lowercase
    """
    if not isinstance(string, str):
        return string
    return string.lower()


def get_select_choices(values, placeholder=None):
    """
    Returns a list of tuple of values an camelcase values
    """
    response = [(value, value.capitalize()) for value in values]
    if placeholder:
        return [('', placeholder)] + response
    return response


class SlugField(wtforms.StringField):
    def __init__(self, label=None, validators=None, filters=None, *args, **kwargs):
        validators = validators or ()
        filters = filters or ()
        validators = tuple(validators) + (validate_unquoted,)
        filters = tuple(filters) + (filter_lowercase,)
        super().__init__(label, validators, filters, *args, **kwargs)


class CentsField(wtforms.StringField):  # Changed from TextField to StringField
    def process_formdata(self, valuelist):
        if not valuelist:
            return
        self.data = decimal.Decimal(valuelist[0]) * 100

    def process_data(self, value):
        if value is not None:  # Add a check for None
            self.data = decimal.Decimal(value)
        else:
            self.data = None
