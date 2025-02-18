from main import app

# Import and register blueprints
from user.views import user_bp
from auth.views import auth_bp
from main.views import main_bp
from listing.views import listing_bp
from payment.views import payment_bp
from activity.views import activity_bp

app.register_blueprint(user_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(main_bp)
app.register_blueprint(listing_bp)
app.register_blueprint(payment_bp)
app.register_blueprint(activity_bp)
