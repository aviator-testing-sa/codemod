#
# forms/common.py
#
#
import decimal
import urllib
import wtforms


def validate_unquoted(form, field):
    """
    True if quoted field is same as unquoted
    """
    return urllib.quote(field.data) == field.data


def filter_lowercase(string):
    """
    Ensure string is lowercase
    """
    if not isinstance(string, basestring):
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
    def __init__(self, label=None, validators=(), filters=(), *args, **kwargs):
        validators = tuple(validators) + (validate_unquoted,)
        filters = tuple(filters) + (filter_lowercase,)
        super(SlugField, self).__init__(label, validators, filters, *args, **kwargs)


class CentsField(wtforms.TextField):
    def process_formdata(self, valuelist):
        if not valuelist:
            return
        self.data = decimal.Decimal(valuelist[0]) * 100

    def process_data(self, value):
        self.data = decimal.Decimal(value)

