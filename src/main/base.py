import datetime
import utils
from main import app

@app.context_processor
def inject_params():
    params = {}
    params['now'] = datetime.datetime.utcnow()
    params.update(app.config)  # Use update() for simpler config injection

    params['image_path'] = image_path
    params['post_date_format'] = post_date_format
    return params

def image_path(path, external=False):
    base_url = '/static/images/'
    if external:
        base_url = app.config['BASE_URL'] + base_url
    return base_url + path

def post_date_format(time):
    return utils.pretty_date(time)
