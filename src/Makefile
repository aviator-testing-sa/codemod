#
# Makefile
#
#
VENV = ../venv
BIN = $(VENV)/bin
PY = $(BIN)/python

default :
	@true

.PHONY : venv
venv : $(VENV)
$(VENV) :
	virtualenv $(VENV)

.PHONY : install
install :
	$(BIN)/pip install -U pip
	$(BIN)/pip install -U -r requirements.txt

# db.migrate: model -> schema
# db.upgrade: apply schema
db.% :
	$(PY) manage.py db $*
