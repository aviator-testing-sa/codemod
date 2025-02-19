import datetime
import utils
from main import app
from flask import url_for

@app.context_processor
def inject_params():
    params = {}
    params['now'] = datetime.datetime.utcnow()
    for key in app.config:
        params[key] = app.config[key]

    params['image_path'] = image_path
    params['post_date_format'] = post_date_format
    return params

def image_path(path, external=False):
    if external:
        return url_for('static', filename=f'images/{path}', _external=True)
    return url_for('static', filename=f'images/{path}')

def post_date_format(time):
    return utils.pretty_date(time)
