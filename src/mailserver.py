"""
Mail server stuff goes hered
"""
# The original code used asyncore and smtpd modules which are deprecated in modern Python.
# Replaced with aiosmtpd which is the recommended replacement.
import contextlib
import asyncio
from aiosmtpd.controller import Controller
import multiprocessing
import threading



class Threaded(object):
    def __init__(self, controller):
        # Changed from asyncore.loop to using aiosmtpd's Controller
        # which handles its own threading
        self.controller = controller
        self.controller.start()

    def close(self):
        # Changed to use controller shutdown instead of asyncore approach
        self.controller.stop()


def debug_server(host, port):
    # Changed from smtpd.DebuggingServer to aiosmtpd's Controller with debugging handler
    from aiosmtpd.handlers import Debugging
    controller = Controller(Debugging(), hostname=host, port=port)
    return Threaded(controller)