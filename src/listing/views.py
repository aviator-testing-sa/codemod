import errors
import emails
import utils
import schema.user
from activity import controller as activity_controller
from auth import buyer_required
from auth import seller_required
from forms.create_listing import CreateBusinessListing
from forms.create_listing import CreateDetailListing
from forms.create_listing import CreateProductListing
from forms.send_message import SendMessageForm
from forms.edit_listing import EditListing
from flask import jsonify
from flask import redirect
from flask import render_template
from flask import request
from flask_login import login_required
from flask_login import current_user
from listing import controller
from main import app


@app.route('/fakelogin')
def fakelogin():
    from schema.user import User
    from auth import views
    views._login_user(User.query.get(1))
    return ''

@app.route('/listing/create/product', methods=['GET', 'POST'])
@login_required
def create_product():
    form = CreateProductListing()
    error = None
    if form.validate_on_submit():
      try:
        listing = controller.create_product(current_user.user, form)
        return redirect('/listing/create/details/' + str(listing.id))
      except errors.ReinventError as e:
        error = e.message

    return render_template('/listing/create_product.html', form=form,
        error=(error or utils.form_errors(form)))


@app.route('/listing/create/details/<listing_id>', methods=['GET', 'POST'])
@login_required
def create_details(listing_id):
    listing = controller.get_listing(listing_id)
    form = CreateDetailListing()
    error = None
    if form.validate_on_submit():
      try:
        controller.create_details(listing, form)
        return redirect('/listing/create/business/' + str(listing.id))
      except errors.ReinventError as e:
        error = e.message
    else:
        return render_template('/listing/create_details.html', form=form, error=error, listing=listing)

@app.post('/listing/upload-screenshot/<listing_id>')
@login_required
def upload_screenshot(listing_id):
    listing = controller.get_listing(listing_id)
    file = request.files.get('file')
    try:
        controller.upload_file(current_user.user, listing, file)
        return jsonify(success=True)
    except errors.ReinventError as e:
        return utils.abort(400, error=e.message)


@app.route('/listing/create/business/<listing_id>', methods=['GET', 'POST'])
@login_required
def create_business(listing_id, status='draft'):
    listing = controller.get_listing(listing_id)
    form = CreateBusinessListing()
    error = None
    if form.validate_on_submit():
      status = 'draft'
      try:
        #if request.form.get('status') != 'SAVE':
        #  status = 'published'
        controller.create_business(listing, form, status)
        emails.listing_complete(current_user.user, listing)
        #if status == 'publish':
        #  auction = controller.publish_listing(current_user.user, listing)
        #  return redirect('/listing/' + auction.id)
        #else:
        return render_template('/listing/thank_you.html', listing=listing)
          #return redirect('/product/' + str(listing.id))
      except errors.ReinventError as e:
        error = e.message
    else:
      return render_template('/listing/create_business.html', form=form, error=error, listing=listing)


@app.get('/product/<listing_id>')
def product(listing_id):
    auction = controller.get_latest_auction(listing_id)
    if auction:
        return redirect('/listing/' + str(auction.id))
    listing = controller.get_listing(listing_id)
    activities = []

    """ Test Data
    from schema.image import ListingImage
    listing.images = [
      ListingImage(url='/static/upload/single_item_01.jpg'),
      ListingImage(url='/static/upload/single_item_02.jpg'),
      ListingImage(url='/static/upload/single_item_03.jpg'),
      ListingImage(url='/static/upload/single_item_04.jpg')
    ]
    from schema.activity import Activity
    import datetime
    activities = [
      Activity(userid=current_user.user.id, user=current_user.user,
        type='auction', status='new', created=datetime.datetime(2016, 10, 25)),
      Activity(userid=current_user.user.id, user=current_user.user,
        type='bid', created=datetime.datetime(2016, 10, 27, 10)),
      Activity(userid=current_user.user.id, user=current_user.user,
        type='bid', created=datetime.datetime(2016, 10, 30, 23)),
      Activity(userid=current_user.user.id, user=current_user.user,
        type='bid', created=datetime.datetime.now()),]
    """
    return render_template('/listing/listing.html', listing=listing, activities=activities)


@app.route('/listing/<auction_id>')
def get_auction(auction_id):
    auction = controller.get_auction(auction_id)
    activities = activity_controller.get_activities(auction)

    return render_template('/listing/listing.html',
      auction=auction, listing=auction.listing, activities=activities)


@app.route('/listing/edit/<listing_id>', methods=['GET', 'POST'])
@login_required
def edit(listing_id):
    listing = controller.get_listing(listing_id)
    form = EditListing(obj=listing)
    if form.validate_on_submit():
      status = request.args.get('status', 'draft')
      try:
        controller.edit_listing(current_user.user, listing, form, status)
        return jsonify(success=True)
      except errors.ReinventError as e:
        return jsonify(success=False, error=e.message)
    else:
        return render_template('/listing/edit.html', listing=listing, form=form)


@app.post('/listing/publish/<listing_id>')
@login_required
def publish(listing_id):
    listing = controller.get_listing(listing_id)
    try:
      auction = controller.publish_listing(current_user.user, listing)
      return jsonify(success=True, auction_id=auction.id)
    except errors.ReinventError as e:
      return jsonify(success=False, error=e.message)


@app.post('/listing/unpublish/<auction_id>')
@login_required
def unpublish(listing_id):
    """ Used to stop the auction """
    listing = controller.get_listing(listing_id)
    try:
        controller.unpublish_listing(current_user.user, listing)
        return jsonify(success=True, listing_id=listing_id)
    except errors.ReinventError as e:
        return jsonify(success=False, error=e.message)


@app.post('/listing/delete/<listing_id>')
@login_required
def delete(listing_id):
    listing = controller.get_listing(listing_id)
    try:
      controller.delete_listing(current_user.user, listing)
      return jsonify(success=True)
    except errors.ReinventError as e:
      return jsonify(success=False, error=e.message)


@app.get('/send_message/<user_id>')
@login_required
def get_send_message(user_id):
    to_user = schema.user.User.get(user_id)
    form = SendMessageForm()
    return render_template('forms/send_message.html', form=form, user=to_user)


@app.post('/send_message/<user_id>')
@login_required
def post_send_message(user_id):
    to_user = schema.user.User.get(user_id)
    form = SendMessageForm()
    if form.validate_on_submit():
      try:
        controller.send_message(current_user.user, form, to_user)
        return jsonify(success=True)
      except errors.ReinventError as e:
        return jsonify(success=False, error=e.message)
    return jsonify(success=False, error=utils.form_errors(form))