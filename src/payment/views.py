from flask import jsonify
from flask import request
from flask_login import current_user
from flask_login import login_required
from main import app
from payment import controller

import logging

@app.route('/customer/add_card', methods=['POST'])
@login_required
def add_card():
    token = request.form.get('stripe_token', '')
    try:
        controller.add_card(current_user.user, token)
        return jsonify(success=True)
    except Exception as e:
        logging.exception(e)
        return jsonify(error="Could not verify card")
