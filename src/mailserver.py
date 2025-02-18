"""
Mail server stuff goes hered
"""
import contextlib
import asyncore
import smtpd
import multiprocessing
import threading


class Threaded(object):
    def __init__(self, smtp):
        self.smtp = smtp
        self.thread = threading.Thread(target=asyncore.loop, kwargs={"timeout": 1})
        self.thread.start()

    def close(self):
        self.smtp.close()
        self.thread.join()


def debug_server(host, port):
    smtp = smtpd.DebuggingServer((host, port), None)
    return Threaded(smtp)
