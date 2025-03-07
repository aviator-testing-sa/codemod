"""empty message

Revision ID: 06b52c05a349
Revises: 4d1dd9ab2c08
Create Date: 2016-10-20 21:13:49.670256

"""

# revision identifiers, used by Alembic.
revision = '06b52c05a349'
down_revision = '4d1dd9ab2c08'

from alembic import op
import sqlalchemy as sa


def upgrade():
    # Changed: Removed use of auto-generated comment markers (###) as this style is deprecated in newer Alembic versions
    op.create_table('activity',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=True),
    sa.Column('created', sa.DateTime(), nullable=True),
    sa.Column('updated', sa.DateTime(), nullable=True),
    sa.Column('auctionid', sa.Integer(), nullable=False),
    sa.Column('userid', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('price_cent', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['auctionid'], ['auction.id'], ),
    sa.ForeignKeyConstraint(['userid'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_activity_type'), 'activity', ['type'], unique=False)
    # Changed: Removed unicode prefix 'u' from strings as it's no longer needed in Python 3 (which SQLAlchemy 2.x targets)
    op.add_column('auction', sa.Column('accepted_bid_id', sa.Integer(), nullable=True))
    op.add_column('auction', sa.Column('close_reason', sa.String(), nullable=True))
    # Changed: Added explicit name for foreign key constraint instead of None as SQLAlchemy 2.x is more strict about constraint naming
    op.create_foreign_key('fk_auction_accepted_bid_id_activity', 'auction', 'activity', ['accepted_bid_id'], ['id'], ondelete='SET NULL')
    # Changed: Removed unicode prefix 'u' from strings
    op.add_column('listing', sa.Column('name', sa.String(), nullable=False))
    op.add_column('listing', sa.Column('web_app', sa.Boolean(), nullable=True))
    op.create_index(op.f('ix_listing_name'), 'listing', ['name'], unique=False)
    # Changed: Removed unicode prefix 'u' from strings
    op.add_column('user', sa.Column('stripe_customer_key', sa.String(), nullable=True))


def downgrade():
    # Changed: Removed use of auto-generated comment markers (###)
    # Changed: Removed unicode prefix 'u' from strings
    op.drop_column('user', 'stripe_customer_key')
    op.drop_index(op.f('ix_listing_name'), table_name='listing')
    op.drop_column('listing', 'web_app')
    op.drop_column('listing', 'name')
    # Changed: Updated constraint name to match the explicit name used in upgrade function
    op.drop_constraint('fk_auction_accepted_bid_id_activity', 'auction', type_='foreignkey')
    op.drop_column('auction', 'close_reason')
    op.drop_column('auction', 'accepted_bid_id')
    op.drop_index(op.f('ix_activity_type'), table_name='activity')
    op.drop_table('activity')