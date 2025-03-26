from wtforms import widgets
from sqlalchemy_utils.types.choice import QuerySelectMultipleField


class MultiCheckboxField(QuerySelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()