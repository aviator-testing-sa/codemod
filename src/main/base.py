import
 datetime
import utils
from main import app


@app.context_processor
def inject_
params():
    params = {}
    params
['now'] = datetime.datetime.utc
now()
    for key in app.
config:
        params[key] = app
.config[key]

    params['image
_path'] = image_path
    params
['post_date_format'] = post_
date_format
    return params

def image
_path(path, external=False):

    base_url = '/static/images/'

    if external:
        base_url
 = app.config['BASE_URL'] +
 base_url
    return base_url +
 path

def post_date_format(time
):
    return utils.pretty_date(
time)
