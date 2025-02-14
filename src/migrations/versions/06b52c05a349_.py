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
    ### commands auto generated by Alembic - please adjust! ###
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
    op.add_column(u'auction', sa.Column('accepted_bid_id', sa.Integer(), nullable=True))
    op.add_column(u'auction', sa.Column('close_reason', sa.String(), nullable=True))
    op.create_foreign_key(None, 'auction', 'activity', ['accepted_bid_id'], ['id'], ondelete='SET NULL')
    op.add_column(u'listing', sa.Column('name', sa.String(), nullable=False))
    op.add_column(u'listing', sa.Column('web_app', sa.Boolean(), nullable=True))
    op.create_index(op.f('ix_listing_name'), 'listing', ['name'], unique=False)
    op.add_column(u'user', sa.Column('stripe_customer_key', sa.String(), nullable=True))
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.drop_column(u'user', 'stripe_customer_key')
    op.drop_index(op.f('ix_listing_name'), table_name='listing')
    op.drop_column(u'listing', 'web_app')
    op.drop_column(u'listing', 'name')
    op.drop_constraint(None, 'auction', type_='foreignkey')
    op.drop_column(u'auction', 'close_reason')
    op.drop_column(u'auction', 'accepted_bid_id')
    op.drop_index(op.f('ix_activity_type'), table_name='activity')
    op.drop_table('activity')
    ### end Alembic commands ###
