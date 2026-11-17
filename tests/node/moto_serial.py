"""moto_server with requests applied one at a time.

moto_server serves requests on parallel threads and moto does not lock items,
so concurrent UpdateItem ADDs can lose updates - something real DynamoDB never
does. The concurrency tests (e.g. cyclepop with 30 parallel callers) then fail
intermittently. Holding a lock around each request gives DynamoDB's atomicity
while the callers still race; connections stay threaded and keep-alive, so it
is no slower in practice.

    python moto_serial.py <port>
"""
import sys
import threading

from moto.server import DomainDispatcherApplication, create_backend_app
from werkzeug.serving import run_simple

_app = DomainDispatcherApplication(create_backend_app)
_lock = threading.Lock()


def app(environ, start_response):
    with _lock:
        # Materialise the body inside the lock so the request is applied whole.
        return list(_app(environ, start_response))


if __name__ == '__main__':
    run_simple('127.0.0.1', int(sys.argv[1]), app, threaded=True, use_reloader=False)
