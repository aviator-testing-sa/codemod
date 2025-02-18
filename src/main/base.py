import datetime
import utils
from flask import current_app

def inject_params():
    params = {}
    params['now'] = datetime.datetime.utcnow()
    for key in current_app.config:
        params[key] = current_app.config[key]

    params['image_path'] = image_path
    params['post_date_format'] = post_date_format
    return params

def image_path(path, external=False):
    base_url = '/static/images/'
    if external:
        base_url = current_app.config['BASE_URL'] + base_url
    return base_url + path

def post_date_format(time):
    return utils.pretty_date(time)

# Assuming you're using a Flask app factory pattern
def create_app():
    app = Flask(__name__)
    # ... other app configurations ...
    app.context_processor(inject_params)
    return app

# Example usage (if not using app factory):
# app = Flask(__name__)
# app.context_processor(inject_params)
