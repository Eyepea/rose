
import collections
import errno
import logging
import pyuv
import socket
import sys

try:
    import ssl
except ImportError:
    ssl = None
try:
    import signal
except ImportError:
    signal = None

from tulip import base_events
from tulip import events
from tulip import futures
from tulip import selector_events
from tulip import tasks


# Errno values indicating the socket isn't ready for I/O just yet.
_TRYAGAIN = frozenset((errno.EAGAIN, errno.EWOULDBLOCK, errno.EINPROGRESS))
if sys.platform == 'win32':
    _TRYAGAIN = frozenset(list(_TRYAGAIN) + [errno.WSAEWOULDBLOCK])

# Argument for default thread pool executor creation.
_MAX_WORKERS = 5


def _noop(*args, **kwargs):
    pass


class Timer(events.Handle):

    def __init__(self, callback, args, timer):
        super().__init__(callback, args)
        self._timer = timer

    def cancel(self):
        super().cancel()
        if self._timer and self._timer.active:
            self._timer.stop()
        self._timer = None


class EventLoop(base_events.BaseEventLoop):

    def __init__(self):
        super().__init__()
        self._loop = pyuv.Loop()
        self._default_executor = None
        self._last_exc = None

        self._fd_map = {}
        self._signal_handlers = {}
        self._ready = collections.deque()
        self._timers = collections.deque()

        self._waker = pyuv.Async(self._loop, _noop)
        self._waker.unref()

        self._ready_processor = pyuv.Check(self._loop)
        self._ready_processor.start(self._process_ready)
        self._ready_processor.unref()

        # Idle handle to control when loop shouldn't block for i/o
        self._ticker = pyuv.Idle(self._loop)

    def run(self):
        if self._running:
            raise RuntimeError('Event loop is running.')
        self._running = True
        try:
            self._run(pyuv.UV_RUN_DEFAULT)
        finally:
            self._running = False

    # run_forever - inherited from BaseEventLoop

    def run_once(self, timeout=None):
        if self._running:
            raise RuntimeError('Event loop is running.')
        self._running = True
        if timeout is not None:
            timer = pyuv.Timer(self._loop)
            timer.start(_noop, timeout, 0)
        try:
            self._run(pyuv.UV_RUN_ONCE)
        finally:
            if timeout is not None:
                timer.close()
            self._running = False

    def run_until_complete(self, future, timeout=None):
        if (not isinstance(future, futures.Future) and tasks.iscoroutine(future)):
            future = tasks.Task(future)
        assert isinstance(future, futures.Future), 'Future is required'
        handler_called = False
        def stop_loop():
            nonlocal handler_called
            handler_called = True
            self.stop()
        future.add_done_callback(lambda _: self.stop())
        if timeout is None:
            self.run_forever()
        else:
            handler = self.call_later(timeout, stop_loop)
            self.run()
            handler.cancel()
        if handler_called:
            raise futures.TimeoutError
        return future.result()

    def stop(self):
        self._loop.stop()
        self._waker.send()

    def close(self):
        self._fd_map.clear()
        self._signal_handlers.clear()
        self._ready.clear()
        self._timers.clear()

        def cb(handle):
            if not handle.closed:
                handle.close()
        self._loop.walk(cb)

        # Run a loop iteration so that close callbacks are called and resources are freed
        assert not self._loop.run(pyuv.UV_RUN_NOWAIT)
        self._loop = None

    # Methods returning Handles for scheduling callbacks.

    def call_later(self, delay, callback, *args):
        if delay <= 0:
            return self.call_soon(callback, *args)
        timer = pyuv.Timer(self._loop)
        handler = Timer(callback, args, timer)
        timer.handler = handler
        timer.start(self._timer_cb, delay, 0)
        self._timers.append(timer)
        return handler

    def call_repeatedly(self, interval, callback, *args):
        if interval <= 0:
            raise ValueError('invalid interval specified: {}'.format(interval))
        timer = pyuv.Timer(self._loop)
        handler = Timer(callback, args, timer)
        timer.handler = handler
        timer.start(self._timer_cb, interval, interval)
        self._timers.append(timer)
        return handler

    def call_soon(self, callback, *args):
        handler = events.make_handle(callback, args)
        self._ready.append(handler)
        if not self._ticker.active:
            self._ticker.start(_noop)
        return handler

    def call_soon_threadsafe(self, callback, *args):
        handler = events.make_handle(callback, args)
        self._ready.append(handler)
        self._waker.send()
        return handler

    # Methods returning Futures for interacting with threads.

    # wrap_future - inherited from BaseEventLoop
    # run_in_executor - inherited from BaseEventLoop
    # set_default_executor - inherited from BaseEventLoop

    # Network I/O methods returning Futures.

    # getaddrinfo - inherited from BaseEventLoop
    # getnameinfo - inherited from BaseEventLoop
    # create_connection - inherited from BaseEventLoop
    # start_serving - inherited from BaseEventLoop
    # start_serving_datagram - inherited from BaseEventLoop

    def _start_serving(self, protocol_factory, sock):
        # Needed by BaseEventLoop.start_serving
        self.add_reader(sock.fileno(), self._accept_connection, protocol_factory, sock)

    def _accept_connection(self, protocol_factory, sock):
        try:
            conn, addr = sock.accept()
        except socket.error as exc:
            if exc.errno in _TRYAGAIN:
                return  # False alarm.
            # Bad error.  Stop serving.
            self.remove_reader(sock.fileno())
            sock.close()
            # There's nowhere to send the error, so just log it.
            # TODO: Someone will want an error handler for this.
            logging.exception('Accept failed')
            return
        protocol = protocol_factory()
        transport = self._make_socket_transport(conn, protocol, extra={'addr': addr})
        # It's now up to the protocol to handle the connection.

    # Level-trigered I/O methods.
    # The add_*() methods return a Handle.
    # The remove_*() methods return True if something was removed,
    # False if there was nothing to delete.

    def add_reader(self, fd, callback, *args):
        handler = events.make_handle(callback, args)
        try:
            poll_h = self._fd_map[fd]
        except KeyError:
            poll_h = self._create_poll_handle(fd)
            self._fd_map[fd] = poll_h
        else:
            poll_h.stop()

        poll_h.pevents |= pyuv.UV_READABLE
        poll_h.read_handler = handler
        poll_h.start(poll_h.pevents, self._poll_cb)

        return handler

    def remove_reader(self, fd):
        try:
            poll_h = self._fd_map[fd]
        except KeyError:
            return False
        else:
            poll_h.stop()
            poll_h.pevents &= ~pyuv.UV_READABLE
            poll_h.read_handler = None
            if poll_h.pevents == 0:
                del self._fd_map[fd]
                poll_h.close()
            else:
                poll_h.start(poll_h.pevents, self._poll_cb)
            return True

    def add_writer(self, fd, callback, *args):
        handler = events.make_handle(callback, args)
        try:
            poll_h = self._fd_map[fd]
        except KeyError:
            poll_h = self._create_poll_handle(fd)
            self._fd_map[fd] = poll_h
        else:
            poll_h.stop()

        poll_h.pevents |= pyuv.UV_WRITABLE
        poll_h.write_handler = handler
        poll_h.start(poll_h.pevents, self._poll_cb)

        return handler

    def remove_writer(self, fd):
        try:
            poll_h = self._fd_map[fd]
        except KeyError:
            return False
        else:
            poll_h.stop()
            poll_h.pevents &= ~pyuv.UV_WRITABLE
            poll_h.write_handler = None
            if poll_h.pevents == 0:
                del self._fd_map[fd]
                poll_h.close()
            else:
                poll_h.start(poll_h.pevents, self._poll_cb)
            return True

    add_connector = add_writer

    remove_connector = remove_writer

    # Completion based I/O methods returning Futures.

    def sock_recv(self, sock, n):
        fut = futures.Future()
        self._sock_recv(fut, False, sock, n)
        return fut

    def _sock_recv(self, fut, registered, sock, n):
        fd = sock.fileno()
        if registered:
            # Remove the callback early.  It should be rare that the
            # selector says the fd is ready but the call still returns
            # EAGAIN, and I am willing to take a hit in that case in
            # order to simplify the common case.
            self.remove_reader(fd)
        if fut.cancelled():
            return
        try:
            data = sock.recv(n)
            fut.set_result(data)
        except socket.error as exc:
            if exc.errno not in _TRYAGAIN:
                fut.set_exception(exc)
            else:
                self.add_reader(fd, self._sock_recv, fut, True, sock, n)

    def sock_sendall(self, sock, data):
        fut = futures.Future()
        self._sock_sendall(fut, False, sock, data)
        return fut

    def _sock_sendall(self, fut, registered, sock, data):
        fd = sock.fileno()
        if registered:
            self.remove_writer(fd)
        if fut.cancelled():
            return
        n = 0
        try:
            if data:
                n = sock.send(data)
        except socket.error as exc:
            if exc.errno not in _TRYAGAIN:
                fut.set_exception(exc)
                return
        if n == len(data):
            fut.set_result(None)
        else:
            if n:
                data = data[n:]
            self.add_writer(fd, self._sock_sendall, fut, True, sock, data)

    def sock_connect(self, sock, address):
        # That address better not require a lookup!  We're not calling
        # self.getaddrinfo() for you here.  But verifying this is
        # complicated; the socket module doesn't have a pattern for
        # IPv6 addresses (there are too many forms, apparently).
        fut = futures.Future()
        self._sock_connect(fut, False, sock, address)
        return fut

    def _sock_connect(self, fut, registered, sock, address):
        fd = sock.fileno()
        if registered:
            self.remove_connector(fd)
        if fut.cancelled():
            return
        try:
            if not registered:
                # First time around.
                sock.connect(address)
            else:
                err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err != 0:
                    # Jump to the except clause below.
                    raise socket.error(err, 'Connect call failed')
            fut.set_result(None)
        except socket.error as exc:
            if exc.errno not in _TRYAGAIN:
                fut.set_exception(exc)
            else:
                self.add_connector(fd, self._sock_connect, fut, True, sock, address)

    def sock_accept(self, sock):
        fut = futures.Future()
        self._sock_accept(fut, False, sock)
        return fut

    def _sock_accept(self, fut, registered, sock):
        fd = sock.fileno()
        if registered:
            self.remove_reader(fd)
        if fut.cancelled():
            return
        try:
            conn, address = sock.accept()
            conn.setblocking(False)
            fut.set_result((conn, address))
        except socket.error as exc:
            if exc.errno not in _TRYAGAIN:
                fut.set_exception(exc)
            else:
                self.add_reader(fd, self._sock_accept, fut, True, sock)

    # Signal handling.

    def add_signal_handler(self, sig, callback, *args):
        self._validate_signal(sig)
        signal_h = pyuv.Signal(self._loop)
        handler = events.make_handle(callback, args)
        signal_h.handler = handler
        try:
            signal_h.start(self._signal_cb, sig)
        except Exception as e:
            signal_h.close()
            raise RuntimeError(str(e))
        else:
            self._signal_handlers[sig] = signal_h
        return handler

    def remove_signal_handler(self, sig):
        self._validate_signal(sig)
        try:
            signal_h = self._signal_handlers.pop(sig)
        except KeyError:
            return False
        del signal_h.handler
        signal_h.close()
        return True

    # Private / internal methods

    def _make_socket_transport(self, sock, protocol, waiter=None, extra=None):
        return selector_events._SelectorSocketTransport(self, sock, protocol, waiter, extra)

    def _make_ssl_transport(self, rawsock, protocol, sslcontext, waiter, extra=None):
        return selector_events._SelectorSslTransport(self, rawsock, protocol, sslcontext, waiter, extra)

    def _make_datagram_transport(self, sock, protocol, address=None, extra=None):
        return selector_events._SelectorDatagramTransport(self, sock, protocol, address, extra)

    def _run(self, mode):
        r = self._loop.run(mode)
        if self._last_exc is not None:
            exc, self._last_exc = self._last_exc, None
            raise exc[1]
        return r

    def _timer_cb(self, timer):
        assert not timer.handler.cancelled
        self._ready.append(timer.handler)
        if not timer.repeat:
            del timer.handler
            self._timers.remove(timer)
            timer.close()

    def _signal_cb(self, signal_h, signum):
        if signal_h.handler.cancelled:
            self.remove_signal_handler(signum)
            return
        self._ready.append(signal_h.handler)

    def _poll_cb(self, poll_h, events, error):
        fd = poll_h.fileno()
        if error is not None:
            # An error happened, signal both readability and writability and
            # let the error propagate
            if poll_h.read_handler is not None:
                if poll_h.read_handler.cancelled:
                    self.remove_reader(fd)
                else:
                    self._ready.append(poll_h.read_handler)
            if poll_h.write_handler is not None:
                if poll_h.write_handler.cancelled:
                    self.remove_writer(fd)
                else:
                    self._ready.append(poll_h.write_handler)
            return

        old_events = poll_h.pevents
        modified = False

        if events & pyuv.UV_READABLE:
            if poll_h.read_handler is not None:
                if poll_h.read_handler.cancelled:
                    self.remove_reader(fd)
                    modified = True
                else:
                    self._ready.append(poll_h.read_handler)
            else:
                poll_h.pevents &= ~pyuv.UV_READABLE
        if events & pyuv.UV_WRITABLE:
            if poll_h.write_handler is not None:
                if poll_h.write_handler.cancelled:
                    self.remove_writer(fd)
                    modified = True
                else:
                    self._ready.append(poll_h.write_handler)
            else:
                poll_h.pevents &= ~pyuv.UV_WRITABLE

        if not modified and old_events != poll_h.pevents:
            # Rearm the handle
            poll_h.stop()
            poll_h.start(poll_h.pevents, self._poll_cb)

    def _process_ready(self, handle):
        # This is the only place where callbacks are actually *called*.
        # All other places just add them to ready.
        # Note: We run all currently scheduled callbacks, but not any
        # callbacks scheduled by callbacks run this time around --
        # they will be run the next time (after another I/O poll).
        # Use an idiom that is threadsafe without using locks.
        ntodo = len(self._ready)
        for i in range(ntodo):
            handler = self._ready.popleft()
            if not handler.cancelled:
                try:
                    handler.callback(*handler.args)
                except Exception:
                    logging.exception('Exception in callback %s %r', handler.callback, handler.args)
                except BaseException:
                    self._last_exc = sys.exc_info()
                    break
        if not self._ready:
            self._ticker.stop()

        # Check for cancelled timers
        for timer in [timer for timer in self._timers if timer.handler.cancelled]:
            timer.close()
            self._timers.remove(timer)
            del timer.handler

    def _create_poll_handle(self, fdobj):
        poll_h = pyuv.Poll(self._loop, self._fileobj_to_fd(fdobj))
        poll_h.pevents = 0
        poll_h.read_handler = None
        poll_h.write_handler = None
        return poll_h

    def _fileobj_to_fd(self, fileobj):
        """Return a file descriptor from a file object.

        Parameters:
        fileobj -- file descriptor, or any object with a `fileno()` method

        Returns:
        corresponding file descriptor
        """
        if isinstance(fileobj, int):
            fd = fileobj
        else:
            try:
                fd = int(fileobj.fileno())
            except (ValueError, TypeError):
                raise ValueError("Invalid file object: {!r}".format(fileobj))
        return fd

    def _validate_signal(self, sig):
        """Internal helper to validate a signal.

        Raise ValueError if the signal number is invalid or uncatchable.
        Raise RuntimeError if there is a problem setting up the handler.
        """
        if not isinstance(sig, int):
            raise TypeError('sig must be an int, not {!r}'.format(sig))
        if signal is None:
            raise RuntimeError('Signals are not supported')
        if not (1 <= sig < signal.NSIG):
            raise ValueError('sig {} out of range(1, {})'.format(sig, signal.NSIG))
        if sys.platform == 'win32':
            raise RuntimeError('Signals are not really supported on Windows')

