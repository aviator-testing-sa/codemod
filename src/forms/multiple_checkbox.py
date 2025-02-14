from wtforms import widgets
from wtforms.ext.sqlalchemy.fields import QuerySelectMultipleField


class MultiCheckboxField(QuerySelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()
