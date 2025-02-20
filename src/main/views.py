import datetime
import random

from flask import redirect
from flask import render_template
from flask import send_from_directory
from flask import url_for
from flask_login import current_user, login_required
from main import app

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/contact')
def contact():
	return redirect('https://docs.google.com/forms/d/e/1FAIpQLSfZTjld-BQgEVdaEn3dNSZAZ2Hq4bTxlgkojsbBeoij8BGD0Q/viewform')

@app.route('/email')
@login_required # Ensure the user is logged in before accessing this route
def email():
	# Access the user object directly from current_user
	user = current_user
	from schema.listing import Listing
	listing = Listing.query.get(6)
	return render_template('emails/welcome.html', listing=listing, user=user)
