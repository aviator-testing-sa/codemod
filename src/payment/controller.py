import stripe
from main import app, db
from schema.user import User

# patch api key
stripe.api_key = app.config['STRIPE_SECRET_KEY']


def add_card(user, token):
    # Check if it's a new customer, then create stripe account,
    # otherwise update card.
    if not user.stripe_customer_key:
        customer = stripe.Customer.create(
            description="Customer for " + user.email,
            email=user.email,
            source=token,
        )
        user.stripe_customer_key = customer.id
        db.session.commit()
    else:
        customer = stripe.Customer.retrieve(user.stripe_customer_key)
        source = stripe.Source.create(type="card", token=token)
        customer.sources.create(source=source.id)
        #stripe_customer.card = token #Removed as card is deprecated
        customer.save()
