# MIGRATION: Changed import from flask.ext.script to flask_script (ext imports are deprecated)
from flask_script import Manager
# MIGRATION: Changed import from flask.ext.migrate to flask_migrate (ext imports are deprecated)
from flask_migrate import Migrate, MigrateCommand
from main import app, db
import os

migrate = Migrate(app, db)
manager = Manager(app)

manager.add_command('db', MigrateCommand)

if __name__ == '__main__':
        manager.run()