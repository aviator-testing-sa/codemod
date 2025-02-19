"""
Mail server stuff goes hered
"""
import asyncio
import smtpd
import multiprocessing
import threading


class AsyncDebuggingServer(smtpd.DebuggingServer):
    async def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
        print('Receiving message from:', peer)
        print('From:', mailfrom)
        print('To:', rcpttos)
        print('Message data:\n')
        print(data.decode('utf8', errors='replace'))
        print('\nDone.')
        return '250 Message accepted for delivery'


class Threaded(object):
    def __init__(self, smtp):
        self.smtp = smtp
        self.thread = threading.Thread(target=self.run_loop)
        self.thread.daemon = True  # Allow the main process to exit even if the thread is running
        self.thread.start()

    def run_loop(self):
        asyncio.set_event_loop(asyncio.new_event_loop())
        asyncio.get_event_loop().run_forever()


    def close(self):
        self.smtp.close()
        self.thread.join(timeout=1)  # give the thread a chance to exit


async def create_debugging_server(host, port):
    loop = asyncio.get_event_loop()
    smtp = AsyncDebuggingServer((host, port), None)
    return smtp


def debug_server(host, port):
    smtp = asyncio.run(create_debugging_server(host, port))
    return Threaded(smtp)
