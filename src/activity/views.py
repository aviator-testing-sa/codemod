import errors
from activity import controller
from flask import jsonify
from flask import render_template
from flask_login import login_required
from flask_login import current_user
from forms.create_bid import CreateBidForm
from forms.submit_interest import SubmitInterestForm
from main import app


@app.route('/bid/create/<auction_id>', methods=['GET', 'POST'])
@login_required
def create_bid(auction_id):
    auction = controller.get_auction(auction_id)
    form = CreateBidForm()
    if form.validate_on_submit():
      try:
        controller.create_bid(current_user.user, auction, form)
        return jsonify(success=True)
      except errors.ReinventError as e:
        return jsonify(success=False, error=e.message)
    return render_template('forms/create_bid.html', form=form, auction=auction)


@app.route('/bid/interest/<auction_id>', methods=['GET', 'POST'])
@login_required
def submit_interest(auction_id):
    auction = controller.get_auction(auction_id)
    form = SubmitInterestForm()
    if form.validate_on_submit():
      try:
        controller.submit_interest(current_user.user, auction, form)
        return jsonify(success=True)
      except errors.ReinventError as e:
        return jsonify(success=False, error=e.message)
    return render_template('forms/submit_interest.html', form=form, auction=auction)


@app.route('/activities/<auction_id>', methods=['GET'])
@login_required
def show_all_activities(auction_id):
    auction = controller.get_auction(auction_id)
    activities = controller.get_activities(auction)
    return render_template('forms/show_all_activities.html',
        auction=auction, activities=activities)
