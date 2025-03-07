import datetime
import random

from flask import redirect
from flask import render_template
from flask import send_from_directory
from flask import url_for
# Updated import path for Flask-Login - flask.ext.* pattern is deprecated
from flask_login import current_user
from flask_login import login_required
from main import app

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/contact')
def contact():
	return redirect('https://docs.google.com/forms/d/e/1FAIpQLSfZTjld-BQgEVdaEn3dNSZAZ2Hq4bTxlgkojsbBeoij8BGD0Q/viewform')


@app.route('/email')
def email():
	user = current_user.user
	from schema.listing import Listing
	# In SQLAlchemy 2.0, query.get() is deprecated in favor of get()
	# When using scoped_session or session directly
	listing = Listing.query.get(6)
	return render_template('emails/welcome.html', listing=listing, user=user)