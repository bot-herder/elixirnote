"""Tornado handlers for kernels.

Preliminary documentation at https://github.com/ipython/ipython/wiki/IPEP-16%3A-Notebook-multi-directory-dashboard-and-URL-mapping#kernels-api
"""
# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
import json
from textwrap import dedent
from traceback import format_tb

from ipython_genutils.py3compat import cast_unicode
from jupyter_client import protocol_version as client_protocol_version

try:
    from jupyter_client.jsonutil import json_default
except ImportError:
    from jupyter_client.jsonutil import date_default as json_default
from tornado import gen
from tornado import web
from tornado.concurrent import Future
from tornado.ioloop import IOLoop

from ...base.handlers import APIHandler
from ...base.zmqhandlers import AuthenticatedZMQStreamHandler
from ...base.zmqhandlers import (
    deserialize_binary_message,
    serialize_msg_to_ws_v1,
    deserialize_msg_from_ws_v1,
)
from jupyter_server.utils import ensure_async
from jupyter_server.utils import url_escape
from jupyter_server.utils import url_path_join
from jupyter_server.auth import authorized


AUTH_RESOURCE = "kernels"


class KernelsAPIHandler(APIHandler):
    auth_resource = AUTH_RESOURCE


class MainKernelHandler(KernelsAPIHandler):
    @web.authenticated
    @authorized
    async def get(self):
        km = self.kernel_manager
        kernels = await ensure_async(km.list_kernels())
        self.finish(json.dumps(kernels, default=json_default))

    @web.authenticated
    @authorized
    async def post(self):
        km = self.kernel_manager
        model = self.get_json_body()
        if model is None:
            model = {"name": km.default_kernel_name}
        else:
            model.setdefault("name", km.default_kernel_name)

        kernel_id = await km.start_kernel(kernel_name=model["name"], path=model.get("path"))
        model = await ensure_async(km.kernel_model(kernel_id))
        location = url_path_join(self.base_url, "api", "kernels", url_escape(kernel_id))
        self.set_header("Location", location)
        self.set_status(201)
        self.finish(json.dumps(model, default=json_default))


class KernelHandler(KernelsAPIHandler):
    @web.authenticated
    @authorized
    async def get(self, kernel_id):
        km = self.kernel_manager
        model = await ensure_async(km.kernel_model(kernel_id))
        self.finish(json.dumps(model, default=json_default))

    @web.authenticated
    @authorized
    async def delete(self, kernel_id):
        km = self.kernel_manager
        await ensure_async(km.shutdown_kernel(kernel_id))
        self.set_status(204)
        self.finish()


class KernelActionHandler(KernelsAPIHandler):
    @web.authenticated
    @authorized
    async def post(self, kernel_id, action):
        km = self.kernel_manager
        if action == "interrupt":
            await ensure_async(km.interrupt_kernel(kernel_id))
            self.set_status(204)
        if action == "restart":

            try:
                await km.restart_kernel(kernel_id)
            except Exception as e:
                message = "Exception restarting kernel"
                self.log.error(message, exc_info=True)
                traceback = format_tb(e.__traceback__)
                self.write(json.dumps(dict(message=message, traceback=traceback)))
                self.set_status(500)
            else:
                model = await ensure_async(km.kernel_model(kernel_id))
                self.write(json.dumps(model, default=json_default))
        self.finish()


class ZMQChannelsHandler(AuthenticatedZMQStreamHandler):
    """There is one ZMQChannelsHandler per running kernel and it oversees all
    the sessions.
    """

    auth_resource = AUTH_RESOURCE

    # class-level registry of open sessions
    # allows checking for conflict on session-id,
    # which is used as a zmq identity and must be unique.
    _open_sessions = {}

    @property
    def kernel_info_timeout(self):
        km_default = self.kernel_manager.kernel_info_timeout
        return self.settings.get("kernel_info_timeout", km_default)

    @property
    def limit_rate(self):
        return self.settings.get("limit_rate", True)

    @property
    def iopub_msg_rate_limit(self):
        return self.settings.get("iopub_msg_rate_limit", 0)

    @property
    def iopub_data_rate_limit(self):
        return self.settings.get("iopub_data_rate_limit", 0)

    @property
    def rate_limit_window(self):
        return self.settings.get("rate_limit_window", 1.0)

    def __repr__(self):
        return "%s(%s)" % (
            self.__class__.__name__,
            getattr(self, "kernel_id", "uninitialized"),
        )

    def create_stream(self):
        km = self.kernel_manager
        identity = self.session.bsession
        for channel in ("iopub", "shell", "control", "stdin"):
            meth = getattr(km, "connect_" + channel)
            self.channels[channel] = stream = meth(self.kernel_id, identity=identity)
            stream.channel = channel

    def nudge(self):
        """Nudge the zmq connections with kernel_info_requests
        Returns a Future that will resolve when we have received
        a shell or control reply and at least one iopub message,
        ensuring that zmq subscriptions are established,
        sockets are fully connected, and kernel is responsive.
        Keeps retrying kernel_info_request until these are both received.
        """
        kernel = self.kernel_manager.get_kernel(self.kernel_id)

        # Do not nudge busy kernels as kernel info requests sent to shell are
        # queued behind execution requests.
        # nudging in this case would cause a potentially very long wait
        # before connections are opened,
        # plus it is *very* unlikely that a busy kernel will not finish
        # establishing its zmq subscriptions before processing the next request.
        if getattr(kernel, "execution_state") == "busy":
            self.log.debug("Nudge: not nudging busy kernel %s", self.kernel_id)
            f = Future()
            f.set_result(None)
            return f
        # Use a transient shell channel to prevent leaking
        # shell responses to the front-end.
        shell_channel = kernel.connect_shell()
        # Use a transient control channel to prevent leaking
        # control responses to the front-end.
        control_channel = kernel.connect_control()
        # The IOPub used by the client, whose subscriptions we are verifying.
        iopub_channel = self.channels["iopub"]

        info_future = Future()
        iopub_future = Future()
        both_done = gen.multi([info_future, iopub_future])

        def finish(_=None):
            """Ensure all futures are resolved
            which in turn triggers cleanup
            """
            for f in (info_future, iopub_future):
                if not f.done():
                    f.set_result(None)

        def cleanup(_=None):
            """Common cleanup"""
            loop.remove_timeout(nudge_handle)
            iopub_channel.stop_on_recv()
            if not shell_channel.closed():
                shell_channel.close()
            if not control_channel.closed():
                control_channel.close()

        # trigger cleanup when both message futures are resolved
        both_done.add_done_callback(cleanup)

        def on_shell_reply(msg):
            self.log.debug("Nudge: shell info reply received: %s", self.kernel_id)
            if not info_future.done():
                self.log.debug("Nudge: resolving shell future: %s", self.kernel_id)
                info_future.set_result(None)

        def on_control_reply(msg):
            self.log.debug("Nudge: control info reply received: %s", self.kernel_id)
            if not info_future.done():
                self.log.debug("Nudge: resolving control future: %s", self.kernel_id)
                info_future.set_result(None)

        def on_iopub(msg):
            self.log.debug("Nudge: IOPub received: %s", self.kernel_id)
            if not iopub_future.done():
                iopub_channel.stop_on_recv()
                self.log.debug("Nudge: resolving iopub future: %s", self.kernel_id)
                iopub_future.set_result(None)

        iopub_channel.on_recv(on_iopub)
        shell_channel.on_recv(on_shell_reply)
        control_channel.on_recv(on_control_reply)
        loop = IOLoop.current()

        # Nudge the kernel with kernel info requests until we get an IOPub message
        def nudge(count):
            count += 1

            # NOTE: this close check appears to never be True during on_open,
            # even when the peer has closed the connection
            if self.ws_connection is None or self.ws_connection.is_closing():
                self.log.debug("Nudge: cancelling on closed websocket: %s", self.kernel_id)
                finish()
                return

            # check for stopped kernel
            if self.kernel_id not in self.kernel_manager:
                self.log.debug("Nudge: cancelling on stopped kernel: %s", self.kernel_id)
                finish()
                return

            # check for closed zmq socket
            if shell_channel.closed():
                self.log.debug("Nudge: cancelling on closed zmq socket: %s", self.kernel_id)
                finish()
                return

            # check for closed zmq socket
            if control_channel.closed():
                self.log.debug("Nudge: cancelling on closed zmq socket: %s", self.kernel_id)
                finish()
                return

            if not both_done.done():
                log = self.log.warning if count % 10 == 0 else self.log.debug
                log("Nudge: attempt %s on kernel %s" % (count, self.kernel_id))
                self.session.send(shell_channel, "kernel_info_request")
                self.session.send(control_channel, "kernel_info_request")
                nonlocal nudge_handle
                nudge_handle = loop.call_later(0.5, nudge, count)

        nudge_handle = loop.call_later(0, nudge, count=0)

        # resolve with a timeout if we get no response
        future = gen.with_timeout(loop.time() + self.kernel_info_timeout, both_done)
        # ensure we have no dangling resources or unresolved Futures in case of timeout
        future.add_done_callback(finish)
        return future

    def request_kernel_info(self):
        """send a request for kernel_info"""
        km = self.kernel_manager
        kernel = km.get_kernel(self.kernel_id)
        try:
            # check for previous request
            future = kernel._kernel_info_future
        except AttributeError:
            self.log.debug("Requesting kernel info from %s", self.kernel_id)
            # Create a kernel_info channel to query the kernel protocol version.
            # This channel will be closed after the kernel_info reply is received.
            if self.kernel_info_channel is None:
                self.kernel_info_channel = km.connect_shell(self.kernel_id)
            self.kernel_info_channel.on_recv(self._handle_kernel_info_reply)
            self.session.send(self.kernel_info_channel, "kernel_info_request")
            # store the future on the kernel, so only one request is sent
            kernel._kernel_info_future = self._kernel_info_future
        else:
            if not future.done():
                self.log.debug("Waiting for pending kernel_info request")
            future.add_done_callback(lambda f: self._finish_kernel_info(f.result()))
        return self._kernel_info_future

    def _handle_kernel_info_reply(self, msg):
        """process the kernel_info_reply

        enabling msg spec adaptation, if necessary
        """
        idents, msg = self.session.feed_identities(msg)
        try:
            msg = self.session.deserialize(msg)
        except:
            self.log.error("Bad kernel_info reply", exc_info=True)
            self._kernel_info_future.set_result({})
            return
        else:
            info = msg["content"]
            self.log.debug("Received kernel info: %s", info)
            if msg["msg_type"] != "kernel_info_reply" or "protocol_version" not in info:
                self.log.error("Kernel info request failed, assuming current %s", info)
                info = {}
            self._finish_kernel_info(info)

        # close the kernel_info channel, we don't need it anymore
        if self.kernel_info_channel:
            self.kernel_info_channel.close()
        self.kernel_info_channel = None

    def _finish_kernel_info(self, info):
        """Finish handling kernel_info reply

        Set up protocol adaptation, if needed,
        and signal that connection can continue.
        """
        protocol_version = info.get("protocol_version", client_protocol_version)
        if protocol_version != client_protocol_version:
            self.session.adapt_version = int(protocol_version.split(".")[0])
            self.log.info(
                "Adapting from protocol version {protocol_version} (kernel {kernel_id}) to {client_protocol_version} (client).".format(
                    protocol_version=protocol_version,
                    kernel_id=self.kernel_id,
                    client_protocol_version=client_protocol_version,
                )
            )
        if not self._kernel_info_future.done():
            self._kernel_info_future.set_result(info)

    def initialize(self):
        super(ZMQChannelsHandler, self).initialize()
        self.zmq_stream = None
        self.channels = {}
        self.kernel_id = None
        self.kernel_info_channel = None
        self._kernel_info_future = Future()
        self._close_future = Future()
        self.session_key = ""

        # Rate limiting code
        self._iopub_window_msg_count = 0
        self._iopub_window_byte_count = 0
        self._iopub_msgs_exceeded = False
        self._iopub_data_exceeded = False
        # Queue of (time stamp, byte count)
        # Allows you to specify that the byte count should be lowered
        # by a delta amount at some point in the future.
        self._iopub_window_byte_queue = []

    async def pre_get(self):
        # authenticate first
        super(ZMQChannelsHandler, self).pre_get()
        # check session collision:
        await self._register_session()
        # then request kernel info, waiting up to a certain time before giving up.
        # We don't want to wait forever, because browsers don't take it well when
        # servers never respond to websocket connection requests.
        kernel = self.kernel_manager.get_kernel(self.kernel_id)

        if hasattr(kernel, "ready"):
            try:
                await kernel.ready
            except Exception as e:
                kernel.execution_state = "dead"
                kernel.reason = str(e)
                raise web.HTTPError(500, str(e)) from e

        self.session.key = kernel.session.key
        future = self.request_kernel_info()

        def give_up():
            """Don't wait forever for the kernel to reply"""
            if future.done():
                return
            self.log.warning("Timeout waiting for kernel_info reply from %s", self.kernel_id)
            future.set_result({})

        loop = IOLoop.current()
        loop.add_timeout(loop.time() + self.kernel_info_timeout, give_up)
        # actually wait for it
        await future

    async def get(self, kernel_id):
        self.kernel_id = cast_unicode(kernel_id, "ascii")
        await super(ZMQChannelsHandler, self).get(kernel_id=kernel_id)

    async def _register_session(self):
        """Ensure we aren't creating a duplicate session.

        If a previous identical session is still open, close it to avoid collisions.
        This is likely due to a client reconnecting from a lost network connection,
        where the socket on our side has not been cleaned up yet.
        """
        self.session_key = "%s:%s" % (self.kernel_id, self.session.session)
        stale_handler = self._open_sessions.get(self.session_key)
        if stale_handler:
            self.log.warning("Replacing stale connection: %s", self.session_key)
            await stale_handler.close()
        if (
            self.kernel_id in self.kernel_manager
        ):  # only update open sessions if kernel is actively managed
            self._open_sessions[self.session_key] = self

    def open(self, kernel_id):
        super(ZMQChannelsHandler, self).open()
        km = self.kernel_manager
        km.notify_connect(kernel_id)

        # on new connections, flush the message buffer
        buffer_info = km.get_buffer(kernel_id, self.session_key)
        if buffer_info and buffer_info["session_key"] == self.session_key:
            self.log.info("Restoring connection for %s", self.session_key)
            if km.ports_changed(kernel_id):
                # If the kernel's ports have changed (some restarts trigger this)
                # then reset the channels so nudge() is using the correct iopub channel
                self.create_stream()
            else:
                # The kernel's ports have not changed; use the channels captured in the buffer
                self.channels = buffer_info["channels"]

            connected = self.nudge()

            def replay(value):
                replay_buffer = buffer_info["buffer"]
                if replay_buffer:
                    self.log.info("Replaying %s buffered messages", len(replay_buffer))
                    for channel, msg_list in replay_buffer:
                        stream = self.channels[channel]
                        self._on_zmq_reply(stream, msg_list)

            connected.add_done_callback(replay)
        else:
            try:
                self.create_stream()
                connected = self.nudge()
            except web.HTTPError as e:
                # Do not log error if the kernel is already shutdown,
                # as it's normal that it's not responding
                try:
                    self.kernel_manager.get_kernel(kernel_id)

                    self.log.error("Error opening stream: %s", e)
                except KeyError:
                    pass
                # WebSockets don't respond to traditional error codes so we
                # close the connection.
                for channel, stream in self.channels.items():
                    if not stream.closed():
                        stream.close()
                self.close()
                return

        km.add_restart_callback(self.kernel_id, self.on_kernel_restarted)
        km.add_restart_callback(self.kernel_id, self.on_restart_failed, "dead")

        def subscribe(value):
            for channel, stream in self.channels.items():
                stream.on_recv_stream(self._on_zmq_reply)

        connected.add_done_callback(subscribe)

        return connected

    def on_message(self, ws_msg):
        if not self.channels:
            # already closed, ignore the message
            self.log.debug("Received message on closed websocket %r", ws_msg)
            return

        if self.selected_subprotocol == "v1.kernel.websocket.jupyter.org":
            channel, msg_list = deserialize_msg_from_ws_v1(ws_msg)
            msg = {
                "header": None,
            }
        else:
            if isinstance(ws_msg, bytes):
                msg = deserialize_binary_message(ws_msg)
            else:
                msg = json.loads(ws_msg)
            msg_list = []
            channel = msg.pop("channel", None)

        if channel is None:
            self.log.warning("No channel specified, assuming shell: %s", msg)
            channel = "shell"
        if channel not in self.channels:
            self.log.warning("No such channel: %r", channel)
            return
        am = self.kernel_manager.allowed_message_types
        ignore_msg = False
        if am:
            msg["header"] = self.get_part("header", msg["header"], msg_list)
            if msg["header"]["msg_type"] not in am:
                self.log.warning(
                    'Received message of type "%s", which is not allowed. Ignoring.'
                    % msg["header"]["msg_type"]
                )
                ignore_msg = True
        if not ignore_msg:
            stream = self.channels[channel]
            if self.selected_subprotocol == "v1.kernel.websocket.jupyter.org":
                self.session.send_raw(stream, msg_list)
            else:
                self.session.send(stream, msg)

    def get_part(self, field, value, msg_list):
        if value is None:
            field2idx = {
                "header": 0,
                "parent_header": 1,
                "content": 3,
            }
            value = self.session.unpack(msg_list[field2idx[field]])
        return value

    def _on_zmq_reply(self, stream, msg_list):
        idents, fed_msg_list = self.session.feed_identities(msg_list)

        if self.selected_subprotocol == "v1.kernel.websocket.jupyter.org":
            msg = {"header": None, "parent_header": None, "content": None}
        else:
            msg = self.session.deserialize(fed_msg_list)

        channel = getattr(stream, "channel", None)
        parts = fed_msg_list[1:]

        self._on_error(channel, msg, parts)

        if self._limit_rate(channel, msg, parts):
            return

        if self.selected_subprotocol == "v1.kernel.websocket.jupyter.org":
            super(ZMQChannelsHandler, self)._on_zmq_reply(stream, parts)
        else:
            super(ZMQChannelsHandler, self)._on_zmq_reply(stream, msg)

    def write_stderr(self, error_message, parent_header):
        self.log.warning(error_message)
        err_msg = self.session.msg(
            "stream",
            content={"text": error_message + "\n", "name": "stderr"},
            parent=parent_header,
        )
        if self.selected_subprotocol == "v1.kernel.websocket.jupyter.org":
            bin_msg = serialize_msg_to_ws_v1(err_msg, "iopub", self.session.pack)
            self.write_message(bin_msg, binary=True)
        else:
            err_msg["channel"] = "iopub"
            self.write_message(json.dumps(err_msg, default=json_default))

    def _limit_rate(self, channel, msg, msg_list):
        if not (self.limit_rate and channel == "iopub"):
            return False

        msg["header"] = self.get_part("header", msg["header"], msg_list)

        msg_type = msg["header"]["msg_type"]
        if msg_type == "status":
            msg["content"] = self.get_part("content", msg["content"], msg_list)
            if msg["content"].get("execution_state") == "idle":
                # reset rate limit counter on status=idle,
                # to avoid 'Run All' hitting limits prematurely.
                self._iopub_window_byte_queue = []
                self._iopub_window_msg_count = 0
                self._iopub_window_byte_count = 0
                self._iopub_msgs_exceeded = False
                self._iopub_data_exceeded = False

        if msg_type not in {"status", "comm_open", "execute_input"}:
            # Remove the counts queued for removal.
            now = IOLoop.current().time()
            while len(self._iopub_window_byte_queue) > 0:
                queued = self._iopub_window_byte_queue[0]
                if now >= queued[0]:
                    self._iopub_window_byte_count -= queued[1]
                    self._iopub_window_msg_count -= 1
                    del self._iopub_window_byte_queue[0]
                else:
                    # This part of the queue hasn't be reached yet, so we can
                    # abort the loop.
                    break

            # Increment the bytes and message count
            self._iopub_window_msg_count += 1
            if msg_type == "stream":
                byte_count = sum([len(x) for x in msg_list])
            else:
                byte_count = 0
            self._iopub_window_byte_count += byte_count

            # Queue a removal of the byte and message count for a time in the
            # future, when we are no longer interested in it.
            self._iopub_window_byte_queue.append((now + self.rate_limit_window, byte_count))

            # Check the limits, set the limit flags, and reset the
            # message and data counts.
            msg_rate = float(self._iopub_window_msg_count) / self.rate_limit_window
            data_rate = float(self._iopub_window_byte_count) / self.rate_limit_window

            # Check the msg rate
            if self.iopub_msg_rate_limit > 0 and msg_rate > self.iopub_msg_rate_limit:
                if not self._iopub_msgs_exceeded:
                    self._iopub_msgs_exceeded = True
                    msg["parent_header"] = self.get_part(
                        "parent_header", msg["parent_header"], msg_list
                    )
                    self.write_stderr(
                        dedent(
                            """\
                    IOPub message rate exceeded.
                    The Jupyter server will temporarily stop sending output
                    to the client in order to avoid crashing it.
                    To change this limit, set the config variable
                    `--ServerApp.iopub_msg_rate_limit`.

                    Current values:
                    ServerApp.iopub_msg_rate_limit={} (msgs/sec)
                    ServerApp.rate_limit_window={} (secs)
                    """.format(
                                self.iopub_msg_rate_limit, self.rate_limit_window
                            )
                        ),
                        msg["parent_header"],
                    )
            else:
                # resume once we've got some headroom below the limit
                if self._iopub_msgs_exceeded and msg_rate < (0.8 * self.iopub_msg_rate_limit):
                    self._iopub_msgs_exceeded = False
                    if not self._iopub_data_exceeded:
                        self.log.warning("iopub messages resumed")

            # Check the data rate
            if self.iopub_data_rate_limit > 0 and data_rate > self.iopub_data_rate_limit:
                if not self._iopub_data_exceeded:
                    self._iopub_data_exceeded = True
                    msg["parent_header"] = self.get_part(
                        "parent_header", msg["parent_header"], msg_list
                    )
                    self.write_stderr(
                        dedent(
                            """\
                    IOPub data rate exceeded.
                    The Jupyter server will temporarily stop sending output
                    to the client in order to avoid crashing it.
                    To change this limit, set the config variable
                    `--ServerApp.iopub_data_rate_limit`.

                    Current values:
                    ServerApp.iopub_data_rate_limit={} (bytes/sec)
                    ServerApp.rate_limit_window={} (secs)
                    """.format(
                                self.iopub_data_rate_limit, self.rate_limit_window
                            )
                        ),
                        msg["parent_header"],
                    )
            else:
                # resume once we've got some headroom below the limit
                if self._iopub_data_exceeded and data_rate < (0.8 * self.iopub_data_rate_limit):
                    self._iopub_data_exceeded = False
                    if not self._iopub_msgs_exceeded:
                        self.log.warning("iopub messages resumed")

            # If either of the limit flags are set, do not send the message.
            if self._iopub_msgs_exceeded or self._iopub_data_exceeded:
                # we didn't send it, remove the current message from the calculus
                self._iopub_window_msg_count -= 1
                self._iopub_window_byte_count -= byte_count
                self._iopub_window_byte_queue.pop(-1)
                return True

            return False

    def close(self):
        super(ZMQChannelsHandler, self).close()
        return self._close_future

    def on_close(self):
        self.log.debug("Websocket closed %s", self.session_key)
        # unregister myself as an open session (only if it's really me)
        if self._open_sessions.get(self.session_key) is self:
            self._open_sessions.pop(self.session_key)

        km = self.kernel_manager
        if self.kernel_id in km:
            km.notify_disconnect(self.kernel_id)
            km.remove_restart_callback(
                self.kernel_id,
                self.on_kernel_restarted,
            )
            km.remove_restart_callback(
                self.kernel_id,
                self.on_restart_failed,
                "dead",
            )

            # start buffering instead of closing if this was the last connection
            if km._kernel_connections[self.kernel_id] == 0:
                km.start_buffering(self.kernel_id, self.session_key, self.channels)
                self._close_future.set_result(None)
                return

        # This method can be called twice, once by self.kernel_died and once
        # from the WebSocket close event. If the WebSocket connection is
        # closed before the ZMQ streams are setup, they could be None.
        for channel, stream in self.channels.items():
            if stream is not None and not stream.closed():
                stream.on_recv(None)
                stream.close()

        self.channels = {}
        self._close_future.set_result(None)

    def _send_status_message(self, status):
        iopub = self.channels.get("iopub", None)
        if iopub and not iopub.closed():
            # flush IOPub before sending a restarting/dead status message
            # ensures proper ordering on the IOPub channel
            # that all messages from the stopped kernel have been delivered
            iopub.flush()
        msg = self.session.msg("status", {"execution_state": status})
        if self.selected_subprotocol == "v1.kernel.websocket.jupyter.org":
            bin_msg = serialize_msg_to_ws_v1(msg, "iopub", self.session.pack)
            self.write_message(bin_msg, binary=True)
        else:
            msg["channel"] = "iopub"
            self.write_message(json.dumps(msg, default=json_default))

    def on_kernel_restarted(self):
        self.log.warning("kernel %s restarted", self.kernel_id)
        self._send_status_message("restarting")

    def on_restart_failed(self):
        self.log.error("kernel %s restarted failed!", self.kernel_id)
        self._send_status_message("dead")

    def _on_error(self, channel, msg, msg_list):
        if self.kernel_manager.allow_tracebacks:
            return

        if channel == "iopub":
            msg["header"] = self.get_part("header", msg["header"], msg_list)
            if msg["header"]["msg_type"] == "error":
                msg["content"] = self.get_part("content", msg["content"], msg_list)
                msg["content"]["ename"] = "ExecutionError"
                msg["content"]["evalue"] = "Execution error"
                msg["content"]["traceback"] = [self.kernel_manager.traceback_replacement_message]
                if self.selected_subprotocol == "v1.kernel.websocket.jupyter.org":
                    msg_list[3] = self.session.pack(msg["content"])


# -----------------------------------------------------------------------------
# URL to handler mappings
# -----------------------------------------------------------------------------


_kernel_id_regex = r"(?P<kernel_id>\w+-\w+-\w+-\w+-\w+)"
_kernel_action_regex = r"(?P<action>restart|interrupt)"

default_handlers = [
    (r"/api/kernels", MainKernelHandler),
    (r"/api/kernels/%s" % _kernel_id_regex, KernelHandler),
    (
        r"/api/kernels/%s/%s" % (_kernel_id_regex, _kernel_action_regex),
        KernelActionHandler,
    ),
    (r"/api/kernels/%s/channels" % _kernel_id_regex, ZMQChannelsHandler),
]