# Copyright 2009-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you
# may not use this file except in compliance with the License.  You
# may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.  See the License for the specific language governing
# permissions and limitations under the License.

"""Tools for connecting to MongoDB.
.. seealso:: :doc:`/examples/high_availability` for examples of connecting
   to replica sets or sets of mongos servers.
To get a :class:`~pymongo.database.Database` instance from a
:class:`MongoClient` use either dictionary-style or attribute-style
access:
.. doctest::
  >>> from pymongo import MongoClient
  >>> c = MongoClient()
  >>> c.test_database
  Database(MongoClient(host=['localhost:27017'], document_class=dict, tz_aware=False, connect=True), u'test_database')
  >>> c['test-database']
  Database(MongoClient(host=['localhost:27017'], document_class=dict, tz_aware=False, connect=True), u'test-database')
"""

import contextlib
import datetime
import threading
import warnings
import weakref

from collections import defaultdict

from bson.codec_options import DEFAULT_CODEC_OPTIONS
from bson.py3compat import (integer_types,
                            string_type)
from bson.son import SON
from pymongo import (common,
                     database,
                     helpers,
                     message,
                     periodic_executor,
                     uri_parser,
                     client_session)
from pymongo.change_stream import ClusterChangeStream
from pymongo.client_options import ClientOptions
from pymongo.command_cursor import CommandCursor
from pymongo.cursor_manager import CursorManager
from pymongo.errors import (AutoReconnect,
                            BulkWriteError,
                            ConfigurationError,
                            ConnectionFailure,
                            InvalidOperation,
                            NotMasterError,
                            OperationFailure,
                            PyMongoError,
                            ServerSelectionTimeoutError)
from pymongo.read_preferences import ReadPreference
from pymongo.server_selectors import (writable_preferred_server_selector,
                                      writable_server_selector)
from pymongo.server_type import SERVER_TYPE
from pymongo.topology import Topology
from pymongo.topology_description import TOPOLOGY_TYPE
from pymongo.settings import TopologySettings
from pymongo.uri_parser import (_handle_option_deprecations,
                                _handle_security_options,
                                _normalize_options)
from pymongo.write_concern import DEFAULT_WRITE_CONCERN

class _ErrorContext(object):
    """An error with context for SDAM error handling."""
    def __init__(self, error, max_wire_version, sock_generation):
        self.error = error
        self.max_wire_version = max_wire_version
        self.sock_generation = sock_generation

class MongoClient(common.BaseObject):

    HOST = "localhost"
    PORT = 27017
    # Define order to retrieve options from ClientOptions for __repr__.
    # No host/port; these are retrieved from TopologySettings.
    _constructor_args = ('document_class', 'tz_aware', 'connect')

    def __init__(
            self,
            host=None,
            port=None,
            document_class=dict,
            tz_aware=None,
            connect=None,
            type_registry=None,
            **kwargs):
     
        if host is None:
            host = self.HOST
        if isinstance(host, string_type):
            host = [host]
        if port is None:
            port = self.PORT
        if not isinstance(port, int):
            raise TypeError("port must be an instance of int")

        # _pool_class, _monitor_class, and _condition_class are for deep
        # customization of PyMongo, e.g. Motor.
        pool_class = kwargs.pop('_pool_class', None)
        monitor_class = kwargs.pop('_monitor_class', None)
        condition_class = kwargs.pop('_condition_class', None)

        # Parse options passed as kwargs.
        keyword_opts = common._CaseInsensitiveDictionary(kwargs)
        keyword_opts['document_class'] = document_class

        seeds = set()
        username = None
        password = None
        dbase = None
        opts = {}
        fqdn = None
        for entity in host:
            if "://" in entity:
                # Determine connection timeout from kwargs.
                timeout = keyword_opts.get("connecttimeoutms")
                if timeout is not None:
                    timeout = common.validate_timeout_or_none(
                        keyword_opts.cased_key("connecttimeoutms"), timeout)
                res = uri_parser.parse_uri(
                    entity, port, validate=True, warn=True, normalize=False,
                    connect_timeout=timeout)
                seeds.update(res["nodelist"])
                username = res["username"] or username
                password = res["password"] or password
                dbase = res["database"] or dbase
                opts = res["options"]
                fqdn = res["fqdn"]
            else:
                seeds.update(uri_parser.split_hosts(entity, port))
        if not seeds:
            raise ConfigurationError("need to specify at least one host")

        # Add options with named keyword arguments to the parsed kwarg options.
        if type_registry is not None:
            keyword_opts['type_registry'] = type_registry
        if tz_aware is None:
            tz_aware = opts.get('tz_aware', False)
        if connect is None:
            connect = opts.get('connect', True)
        keyword_opts['tz_aware'] = tz_aware
        keyword_opts['connect'] = connect

        # Handle deprecated options in kwarg options.
        keyword_opts = _handle_option_deprecations(keyword_opts)
        # Validate kwarg options.
        keyword_opts = common._CaseInsensitiveDictionary(dict(common.validate(
            keyword_opts.cased_key(k), v) for k, v in keyword_opts.items()))

        # Override connection string options with kwarg options.
        opts.update(keyword_opts)
        # Handle security-option conflicts in combined options.
        opts = _handle_security_options(opts)
        # Normalize combined options.
        opts = _normalize_options(opts)

        # Username and password passed as kwargs override user info in URI.
        username = opts.get("username", username)
        password = opts.get("password", password)
        if 'socketkeepalive' in opts:
            warnings.warn(
                "The socketKeepAlive option is deprecated. It now"
                "defaults to true and disabling it is not recommended, see "
                "https://docs.mongodb.com/manual/faq/diagnostics/"
                "#does-tcp-keepalive-time-affect-mongodb-deployments",
                DeprecationWarning, stacklevel=2)
        self.__options = options = ClientOptions(
            username, password, dbase, opts)

        self.__default_database_name = dbase
        self.__lock = threading.Lock()
        self.__cursor_manager = None
        self.__kill_cursors_queue = []

        self._event_listeners = options.pool_options.event_listeners

        # Cache of existing indexes used by ensure_index ops.
        self.__index_cache = {}
        self.__index_cache_lock = threading.Lock()

        super(MongoClient, self).__init__(options.codec_options,
                                          options.read_preference,
                                          options.write_concern,
                                          options.read_concern)

        self.__all_credentials = {}
        creds = options.credentials
        if creds:
            self._cache_credentials(creds.source, creds)

        self._topology_settings = TopologySettings(
            seeds=seeds,
            replica_set_name=options.replica_set_name,
            pool_class=pool_class,
            pool_options=options.pool_options,
            monitor_class=monitor_class,
            condition_class=condition_class,
            local_threshold_ms=options.local_threshold_ms,
            server_selection_timeout=options.server_selection_timeout,
            server_selector=options.server_selector,
            heartbeat_frequency=options.heartbeat_frequency,
            fqdn=fqdn)

        self._topology = Topology(self._topology_settings)
        if connect:
            self._topology.open()

        def target():
            client = self_ref()
            if client is None:
                return False  # Stop the executor.
            MongoClient._process_periodic_tasks(client)
            return True

        executor = periodic_executor.PeriodicExecutor(
            interval=common.KILL_CURSOR_FREQUENCY,
            min_interval=0.5,
            target=target,
            name="pymongo_kill_cursors_thread")

        # We strongly reference the executor and it weakly references us via
        # this closure. When the client is freed, stop the executor soon.
        self_ref = weakref.ref(self, executor.close)
        self._kill_cursors_executor = executor
        executor.open()

        self._encrypter = None
        if self.__options.auto_encryption_opts:
            from pymongo.encryption import _Encrypter
            self._encrypter = _Encrypter.create(
                self, self.__options.auto_encryption_opts)

    def _cache_credentials(self, source, credentials, connect=False):
        """Save a set of authentication credentials.
        The credentials are used to login a socket whenever one is created.
        If `connect` is True, verify the credentials on the server first.
        """
        # Don't let other threads affect this call's data.
        all_credentials = self.__all_credentials.copy()

        if source in all_credentials:
            # Nothing to do if we already have these credentials.
            if credentials == all_credentials[source]:
                return
            raise OperationFailure('Another user is already authenticated '
                                   'to this database. You must logout first.')

        if connect:
            server = self._get_topology().select_server(
                writable_preferred_server_selector)

            # get_socket() logs out of the database if logged in with old
            # credentials, and logs in with new ones.
            with server.get_socket(all_credentials) as sock_info:
                sock_info.authenticate(credentials)

        # If several threads run _cache_credentials at once, last one wins.
        self.__all_credentials[source] = credentials

    def _purge_credentials(self, source):
        """Purge credentials from the authentication cache."""
        self.__all_credentials.pop(source, None)

    def _cached(self, dbname, coll, index):
        """Test if `index` is cached."""
        cache = self.__index_cache
        now = datetime.datetime.utcnow()
        with self.__index_cache_lock:
            return (dbname in cache and
                    coll in cache[dbname] and
                    index in cache[dbname][coll] and
                    now < cache[dbname][coll][index])

    def _cache_index(self, dbname, collection, index, cache_for):
        """Add an index to the index cache for ensure_index operations."""
        now = datetime.datetime.utcnow()
        expire = datetime.timedelta(seconds=cache_for) + now

        with self.__index_cache_lock:
            if dbname not in self.__index_cache:
                self.__index_cache[dbname] = {}
                self.__index_cache[dbname][collection] = {}
                self.__index_cache[dbname][collection][index] = expire

            elif collection not in self.__index_cache[dbname]:
                self.__index_cache[dbname][collection] = {}
                self.__index_cache[dbname][collection][index] = expire

            else:
                self.__index_cache[dbname][collection][index] = expire

    def _purge_index(self, database_name,
                     collection_name=None, index_name=None):
        """Purge an index from the index cache.
        If `index_name` is None purge an entire collection.
        If `collection_name` is None purge an entire database.
        """
        with self.__index_cache_lock:
            if not database_name in self.__index_cache:
                return

            if collection_name is None:
                del self.__index_cache[database_name]
                return

            if not collection_name in self.__index_cache[database_name]:
                return

            if index_name is None:
                del self.__index_cache[database_name][collection_name]
                return

            if index_name in self.__index_cache[database_name][collection_name]:
                del self.__index_cache[database_name][collection_name][index_name]

    def _server_property(self, attr_name):
        """An attribute of the current server's description.
        If the client is not connected, this will block until a connection is
        established or raise ServerSelectionTimeoutError if no server is
        available.
        Not threadsafe if used multiple times in a single method, since
        the server may change. In such cases, store a local reference to a
        ServerDescription first, then use its properties.
        """
        server = self._topology.select_server(
            writable_server_selector)

        return getattr(server.description, attr_name)

    def watch(self, pipeline=None, full_document=None, resume_after=None,
              max_await_time_ms=None, batch_size=None, collation=None,
              start_at_operation_time=None, session=None, start_after=None):
        """Watch changes on this cluster.
        Performs an aggregation with an implicit initial ``$changeStream``
        stage and returns a
        :class:`~pymongo.change_stream.ClusterChangeStream` cursor which
        iterates over changes on all databases on this cluster.
        Introduced in MongoDB 4.0.
        .. code-block:: python
           with client.watch() as stream:
               for change in stream:
                   print(change)
        The :class:`~pymongo.change_stream.ClusterChangeStream` iterable
        blocks until the next change document is returned or an error is
        raised. If the
        :meth:`~pymongo.change_stream.ClusterChangeStream.next` method
        encounters a network error when retrieving a batch from the server,
        it will automatically attempt to recreate the cursor such that no
        change events are missed. Any error encountered during the resume
        attempt indicates there may be an outage and will be raised.
        .. code-block:: python
            try:
                with client.watch(
                        [{'$match': {'operationType': 'insert'}}]) as stream:
                    for insert_change in stream:
                        print(insert_change)
            except pymongo.errors.PyMongoError:
                # The ChangeStream encountered an unrecoverable error or the
                # resume attempt failed to recreate the cursor.
                logging.error('...')
        For a precise description of the resume process see the
        `change streams specification`_.
        :Parameters:
          - `pipeline` (optional): A list of aggregation pipeline stages to
            append to an initial ``$changeStream`` stage. Not all
            pipeline stages are valid after a ``$changeStream`` stage, see the
            MongoDB documentation on change streams for the supported stages.
          - `full_document` (optional): The fullDocument to pass as an option
            to the ``$changeStream`` stage. Allowed values: 'updateLookup'.
            When set to 'updateLookup', the change notification for partial
            updates will include both a delta describing the changes to the
            document, as well as a copy of the entire document that was
            changed from some time after the change occurred.
          - `resume_after` (optional): A resume token. If provided, the
            change stream will start returning changes that occur directly
            after the operation specified in the resume token. A resume token
            is the _id value of a change document.
          - `max_await_time_ms` (optional): The maximum time in milliseconds
            for the server to wait for changes before responding to a getMore
            operation.
          - `batch_size` (optional): The maximum number of documents to return
            per batch.
          - `collation` (optional): The :class:`~pymongo.collation.Collation`
            to use for the aggregation.
          - `start_at_operation_time` (optional): If provided, the resulting
            change stream will only return changes that occurred at or after
            the specified :class:`~bson.timestamp.Timestamp`. Requires
            MongoDB >= 4.0.
          - `session` (optional): a
            :class:`~pymongo.client_session.ClientSession`.
          - `start_after` (optional): The same as `resume_after` except that
            `start_after` can resume notifications after an invalidate event.
            This option and `resume_after` are mutually exclusive.
        :Returns:
          A :class:`~pymongo.change_stream.ClusterChangeStream` cursor.
        .. versionchanged:: 3.9
           Added the ``start_after`` parameter.
        .. versionadded:: 3.7
        .. mongodoc:: changeStreams
        .. _change streams specification:
            https://github.com/mongodb/specifications/blob/master/source/change-streams/change-streams.rst
        """
        return ClusterChangeStream(
            self.admin, pipeline, full_document, resume_after, max_await_time_ms,
            batch_size, collation, start_at_operation_time, session,
            start_after)

    @property
    def event_listeners(self):
        """The event listeners registered for this client.
        See :mod:`~pymongo.monitoring` for details.
        """
        return self._event_listeners.event_listeners

    @property
    def address(self):
        """(host, port) of the current standalone, primary, or mongos, or None.
        Accessing :attr:`address` raises :exc:`~.errors.InvalidOperation` if
        the client is load-balancing among mongoses, since there is no single
        address. Use :attr:`nodes` instead.
        If the client is not connected, this will block until a connection is
        established or raise ServerSelectionTimeoutError if no server is
        available.
        .. versionadded:: 3.0
        """
        topology_type = self._topology._description.topology_type
        if topology_type == TOPOLOGY_TYPE.Sharded:
            raise InvalidOperation(
                'Cannot use "address" property when load balancing among'
                ' mongoses, use "nodes" instead.')
        if topology_type not in (TOPOLOGY_TYPE.ReplicaSetWithPrimary,
                                 TOPOLOGY_TYPE.Single):
            return None
        return self._server_property('address')

    @property
    def primary(self):
        """The (host, port) of the current primary of the replica set.
        Returns ``None`` if this client is not connected to a replica set,
        there is no primary, or this client was created without the
        `replicaSet` option.
        .. versionadded:: 3.0
           MongoClient gained this property in version 3.0 when
           MongoReplicaSetClient's functionality was merged in.
        """
        return self._topology.get_primary()

    @property
    def secondaries(self):
        """The secondary members known to this client.
        A sequence of (host, port) pairs. Empty if this client is not
        connected to a replica set, there are no visible secondaries, or this
        client was created without the `replicaSet` option.
        .. versionadded:: 3.0
           MongoClient gained this property in version 3.0 when
           MongoReplicaSetClient's functionality was merged in.
        """
        return self._topology.get_secondaries()

    @property
    def arbiters(self):
        """Arbiters in the replica set.
        A sequence of (host, port) pairs. Empty if this client is not
        connected to a replica set, there are no arbiters, or this client was
        created without the `replicaSet` option.
        """
        return self._topology.get_arbiters()

    @property
    def is_primary(self):
        """If this client is connected to a server that can accept writes.
        True if the current server is a standalone, mongos, or the primary of
        a replica set. If the client is not connected, this will block until a
        connection is established or raise ServerSelectionTimeoutError if no
        server is available.
        """
        return self._server_property('is_writable')

    @property
    def is_mongos(self):
        """If this client is connected to mongos. If the client is not
        connected, this will block until a connection is established or raise
        ServerSelectionTimeoutError if no server is available..
        """
        return self._server_property('server_type') == SERVER_TYPE.Mongos

    @property
    def max_pool_size(self):
        """The maximum allowable number of concurrent connections to each
        connected server. Requests to a server will block if there are
        `maxPoolSize` outstanding connections to the requested server.
        Defaults to 100. Cannot be 0.
        When a server's pool has reached `max_pool_size`, operations for that
        server block waiting for a socket to be returned to the pool. If
        ``waitQueueTimeoutMS`` is set, a blocked operation will raise
        :exc:`~pymongo.errors.ConnectionFailure` after a timeout.
        By default ``waitQueueTimeoutMS`` is not set.
        """
        return self.__options.pool_options.max_pool_size

    @property
    def min_pool_size(self):
        """The minimum required number of concurrent connections that the pool
        will maintain to each connected server. Default is 0.
        """
        return self.__options.pool_options.min_pool_size

    @property
    def max_idle_time_ms(self):
        """The maximum number of milliseconds that a connection can remain
        idle in the pool before being removed and replaced. Defaults to
        `None` (no limit).
        """
        seconds = self.__options.pool_options.max_idle_time_seconds
        if seconds is None:
            return None
        return 1000 * seconds

    @property
    def nodes(self):
        """Set of all currently connected servers.
        .. warning:: When connected to a replica set the value of :attr:`nodes`
          can change over time as :class:`MongoClient`'s view of the replica
          set changes. :attr:`nodes` can also be an empty set when
          :class:`MongoClient` is first instantiated and hasn't yet connected
          to any servers, or a network partition causes it to lose connection
          to all servers.
        """
        description = self._topology.description
        return frozenset(s.address for s in description.known_servers)

    @property
    def max_bson_size(self):
        """The largest BSON object the connected server accepts in bytes.
        If the client is not connected, this will block until a connection is
        established or raise ServerSelectionTimeoutError if no server is
        available.
        """
        return self._server_property('max_bson_size')

    @property
    def max_message_size(self):
        """The largest message the connected server accepts in bytes.
        If the client is not connected, this will block until a connection is
        established or raise ServerSelectionTimeoutError if no server is
        available.
        """
        return self._server_property('max_message_size')

    @property
    def max_write_batch_size(self):
        """The maxWriteBatchSize reported by the server.
        If the client is not connected, this will block until a connection is
        established or raise ServerSelectionTimeoutError if no server is
        available.
        Returns a default value when connected to server versions prior to
        MongoDB 2.6.
        """
        return self._server_property('max_write_batch_size')

    @property
    def local_threshold_ms(self):
        """The local threshold for this instance."""
        return self.__options.local_threshold_ms

    @property
    def server_selection_timeout(self):
        """The server selection timeout for this instance in seconds."""
        return self.__options.server_selection_timeout

    @property
    def retry_writes(self):
        """If this instance should retry supported write operations."""
        return self.__options.retry_writes

    @property
    def retry_reads(self):
        """If this instance should retry supported write operations."""
        return self.__options.retry_reads

    def _is_writable(self):
        """Attempt to connect to a writable server, or return False.
        """
        topology = self._get_topology()  # Starts monitors if necessary.
        try:
            svr = topology.select_server(writable_server_selector)

            # When directly connected to a secondary, arbiter, etc.,
            # select_server returns it, whatever the selector. Check
            # again if the server is writable.
            return svr.description.is_writable
        except ConnectionFailure:
            return False

    def _end_sessions(self, session_ids):
        """Send endSessions command(s) with the given session ids."""
        try:
            # Use SocketInfo.command directly to avoid implicitly creating
            # another session.
            with self._socket_for_reads(
                    ReadPreference.PRIMARY_PREFERRED,
                    None) as (sock_info, slave_ok):
                if not sock_info.supports_sessions:
                    return

                for i in range(0, len(session_ids), common._MAX_END_SESSIONS):
                    spec = SON([('endSessions',
                                 session_ids[i:i + common._MAX_END_SESSIONS])])
                    sock_info.command(
                        'admin', spec, slave_ok=slave_ok, client=self)
        except PyMongoError:
            # Drivers MUST ignore any errors returned by the endSessions
            # command.
            pass

    def close(self):
        """Cleanup client resources and disconnect from MongoDB.
        On MongoDB >= 3.6, end all server sessions created by this client by
        sending one or more endSessions commands.
        Close all sockets in the connection pools and stop the monitor threads.
        If this instance is used again it will be automatically re-opened and
        the threads restarted unless auto encryption is enabled. A client
        enabled with auto encryption cannot be used again after being closed;
        any attempt will raise :exc:`~.errors.InvalidOperation`.
        .. versionchanged:: 3.6
           End all server sessions created by this client.
        """
        session_ids = self._topology.pop_all_sessions()
        if session_ids:
            self._end_sessions(session_ids)
        # Stop the periodic task thread and then send pending killCursor
        # requests before closing the topology.
        self._kill_cursors_executor.close()
        self._process_kill_cursors()
        self._topology.close()
        if self._encrypter:
            # TODO: PYTHON-1921 Encrypted MongoClients cannot be re-opened.
            self._encrypter.close()

    def set_cursor_manager(self, manager_class):
        """DEPRECATED - Set this client's cursor manager.
        Raises :class:`TypeError` if `manager_class` is not a subclass of
        :class:`~pymongo.cursor_manager.CursorManager`. A cursor manager
        handles closing cursors. Different managers can implement different
        policies in terms of when to actually kill a cursor that has
        been closed.
        :Parameters:
          - `manager_class`: cursor manager to use
        .. versionchanged:: 3.3
           Deprecated, for real this time.
        .. versionchanged:: 3.0
           Undeprecated.
        """
        warnings.warn(
            "set_cursor_manager is Deprecated",
            DeprecationWarning,
            stacklevel=2)
        manager = manager_class(self)
        if not isinstance(manager, CursorManager):
            raise TypeError("manager_class must be a subclass of "
                            "CursorManager")

        self.__cursor_manager = manager

    def _get_topology(self):
        """Get the internal :class:`~pymongo.topology.Topology` object.
        If this client was created with "connect=False", calling _get_topology
        launches the connection process in the background.
        """
        self._topology.open()
        with self.__lock:
            self._kill_cursors_executor.open()
        return self._topology

    @contextlib.contextmanager
    def _get_socket(self, server, session, exhaust=False):
        with _MongoClientErrorHandler(self, server, session) as err_handler:
            with server.get_socket(
                    self.__all_credentials, checkout=exhaust) as sock_info:
                err_handler.contribute_socket(sock_info)
                if (self._encrypter and
                        not self._encrypter._bypass_auto_encryption and
                        sock_info.max_wire_version < 8):
                    raise ConfigurationError(
                        'Auto-encryption requires a minimum MongoDB version '
                        'of 4.2')
                yield sock_info

    def _select_server(self, server_selector, session, address=None):
        """Select a server to run an operation on this client.
        :Parameters:
          - `server_selector`: The server selector to use if the session is
            not pinned and no address is given.
          - `session`: The ClientSession for the next operation, or None. May
            be pinned to a mongos server address.
          - `address` (optional): Address when sending a message
            to a specific server, used for getMore.
        """
        try:
            topology = self._get_topology()
            address = address or (session and session._pinned_address)
            if address:
                # We're running a getMore or this session is pinned to a mongos.
                server = topology.select_server_by_address(address)
                if not server:
                    raise AutoReconnect('server %s:%d no longer available'
                                        % address)
            else:
                server = topology.select_server(server_selector)
                # Pin this session to the selected server if it's performing a
                # sharded transaction.
                if server.description.mongos and (session and
                                                  session.in_transaction):
                    session._pin_mongos(server)
            return server
        except PyMongoError as exc:
            # Server selection errors in a transaction are transient.
            if session and session.in_transaction:
                exc._add_error_label("TransientTransactionError")
                session._unpin_mongos()
            raise

    def _socket_for_writes(self, session):
        server = self._select_server(writable_server_selector, session)
        return self._get_socket(server, session)

    @contextlib.contextmanager
    def _slaveok_for_server(self, read_preference, server, session,
                            exhaust=False):
        assert read_preference is not None, "read_preference must not be None"
        # Get a socket for a server matching the read preference, and yield
        # sock_info, slave_ok. Server Selection Spec: "slaveOK must be sent to
        # mongods with topology type Single. If the server type is Mongos,
        # follow the rules for passing read preference to mongos, even for
        # topology type Single."
        # Thread safe: if the type is single it cannot change.
        topology = self._get_topology()
        single = topology.description.topology_type == TOPOLOGY_TYPE.Single

        with self._get_socket(server, session, exhaust=exhaust) as sock_info:
            slave_ok = (single and not sock_info.is_mongos) or (
                read_preference != ReadPreference.PRIMARY)
            yield sock_info, slave_ok

    @contextlib.contextmanager
    def _socket_for_reads(self, read_preference, session):
        assert read_preference is not None, "read_preference must not be None"
        # Get a socket for a server matching the read preference, and yield
        # sock_info, slave_ok. Server Selection Spec: "slaveOK must be sent to
        # mongods with topology type Single. If the server type is Mongos,
        # follow the rules for passing read preference to mongos, even for
        # topology type Single."
        # Thread safe: if the type is single it cannot change.
        topology = self._get_topology()
        single = topology.description.topology_type == TOPOLOGY_TYPE.Single
        server = self._select_server(read_preference, session)

        with self._get_socket(server, session) as sock_info:
            slave_ok = (single and not sock_info.is_mongos) or (
                read_preference != ReadPreference.PRIMARY)
            yield sock_info, slave_ok

    def _run_operation_with_response(self, operation, unpack_res,
                                     exhaust=False, address=None):
        """Run a _Query/_GetMore operation and return a Response.
        :Parameters:
          - `operation`: a _Query or _GetMore object.
          - `unpack_res`: A callable that decodes the wire protocol response.
          - `exhaust` (optional): If True, the socket used stays checked out.
            It is returned along with its Pool in the Response.
          - `address` (optional): Optional address when sending a message
            to a specific server, used for getMore.
        """
        if operation.exhaust_mgr:
            server = self._select_server(
                operation.read_preference, operation.session, address=address)

            with _MongoClientErrorHandler(
                    self, server, operation.session) as err_handler:
                err_handler.contribute_socket(operation.exhaust_mgr.sock)
                return server.run_operation_with_response(
                    operation.exhaust_mgr.sock,
                    operation,
                    True,
                    self._event_listeners,
                    exhaust,
                    unpack_res)

        def _cmd(session, server, sock_info, slave_ok):
            return server.run_operation_with_response(
                sock_info,
                operation,
                slave_ok,
                self._event_listeners,
                exhaust,
                unpack_res)

        return self._retryable_read(
            _cmd, operation.read_preference, operation.session,
            address=address,
            retryable=isinstance(operation, message._Query),
            exhaust=exhaust)

    def _retry_with_session(self, retryable, func, session, bulk):
        """Execute an operation with at most one consecutive retries
        Returns func()'s return value on success. On error retries the same
        command once.
        Re-raises any exception thrown by func().
        """
        retryable = (retryable and self.retry_writes
                     and session and not session.in_transaction)
        return self._retry_internal(retryable, func, session, bulk)

    def _retry_internal(self, retryable, func, session, bulk):
        """Internal retryable write helper."""
        max_wire_version = 0
        last_error = None
        retrying = False

        def is_retrying():
            return bulk.retrying if bulk else retrying
        # Increment the transaction id up front to ensure any retry attempt
        # will use the proper txnNumber, even if server or socket selection
        # fails before the command can be sent.
        if retryable and session and not session.in_transaction:
            session._start_retryable_write()
            if bulk:
                bulk.started_retryable_write = True

        while True:
            try:
                server = self._select_server(writable_server_selector, session)
                supports_session = (
                    session is not None and
                    server.description.retryable_writes_supported)
                with self._get_socket(server, session) as sock_info:
                    max_wire_version = sock_info.max_wire_version
                    if retryable and not supports_session:
                        if is_retrying():
                            # A retry is not possible because this server does
                            # not support sessions raise the last error.
                            raise last_error
                        retryable = False
                    return func(session, sock_info, retryable)
            except ServerSelectionTimeoutError:
                if is_retrying():
                    # The application may think the write was never attempted
                    # if we raise ServerSelectionTimeoutError on the retry
                    # attempt. Raise the original exception instead.
                    raise last_error
                # A ServerSelectionTimeoutError error indicates that there may
                # be a persistent outage. Attempting to retry in this case will
                # most likely be a waste of time.
                raise
            except Exception as exc:
                if not retryable:
                    raise
                # Add the RetryableWriteError label.
                if (not _retryable_writes_error(exc, max_wire_version)
                        or is_retrying()):
                    raise
                if bulk:
                    bulk.retrying = True
                else:
                    retrying = True
                last_error = exc

    def _retryable_read(self, func, read_pref, session, address=None,
                        retryable=True, exhaust=False):
        """Execute an operation with at most one consecutive retries
        Returns func()'s return value on success. On error retries the same
        command once.
        Re-raises any exception thrown by func().
        """
        retryable = (retryable and
                     self.retry_reads
                     and not (session and session.in_transaction))
        last_error = None
        retrying = False

        while True:
            try:
                server = self._select_server(
                    read_pref, session, address=address)
                if not server.description.retryable_reads_supported:
                    retryable = False
                with self._slaveok_for_server(read_pref, server, session,
                                              exhaust=exhaust) as (sock_info,
                                                                   slave_ok):
                    if retrying and not retryable:
                        # A retry is not possible because this server does
                        # not support retryable reads, raise the last error.
                        raise last_error
                    return func(session, server, sock_info, slave_ok)
            except ServerSelectionTimeoutError:
                if retrying:
                    # The application may think the write was never attempted
                    # if we raise ServerSelectionTimeoutError on the retry
                    # attempt. Raise the original exception instead.
                    raise last_error
                # A ServerSelectionTimeoutError error indicates that there may
                # be a persistent outage. Attempting to retry in this case will
                # most likely be a waste of time.
                raise
            except ConnectionFailure as exc:
                if not retryable or retrying:
                    raise
                retrying = True
                last_error = exc
            except OperationFailure as exc:
                if not retryable or retrying:
                    raise
                if exc.code not in helpers._RETRYABLE_ERROR_CODES:
                    raise
                retrying = True
                last_error = exc

    def _retryable_write(self, retryable, func, session):
        """Internal retryable write helper."""
        with self._tmp_session(session) as s:
            return self._retry_with_session(retryable, func, s, None)

    def _handle_getlasterror(self, address, error_msg):
        """Clear our pool for a server, mark it Unknown, and check it soon."""
        self._topology.handle_getlasterror(address, error_msg)

    def __eq__(self, other):
        if isinstance(other, self.__class__):
            return self.address == other.address
        return NotImplemented

    def __ne__(self, other):
        return not self == other

    def _repr_helper(self):
        def option_repr(option, value):
            """Fix options whose __repr__ isn't usable in a constructor."""
            if option == 'document_class':
                if value is dict:
                    return 'document_class=dict'
                else:
                    return 'document_class=%s.%s' % (value.__module__,
                                                     value.__name__)
            if option in common.TIMEOUT_OPTIONS and value is not None:
                return "%s=%s" % (option, int(value * 1000))

            return '%s=%r' % (option, value)

        # Host first...
        options = ['host=%r' % [
            '%s:%d' % (host, port) if port is not None else host
            for host, port in self._topology_settings.seeds]]
        # ... then everything in self._constructor_args...
        options.extend(
            option_repr(key, self.__options._options[key])
            for key in self._constructor_args)
        # ... then everything else.
        options.extend(
            option_repr(key, self.__options._options[key])
            for key in self.__options._options
            if key not in set(self._constructor_args)
            and key != 'username' and key != 'password')
        return ', '.join(options)

    def __repr__(self):
        return ("MongoClient(%s)" % (self._repr_helper(),))

    def __getattr__(self, name):
        """Get a database by name.
        Raises :class:`~pymongo.errors.InvalidName` if an invalid
        database name is used.
        :Parameters:
          - `name`: the name of the database to get
        """
        if name.startswith('_'):
            raise AttributeError(
                "MongoClient has no attribute %r. To access the %s"
                " database, use client[%r]." % (name, name, name))
        return self.__getitem__(name)

    def __getitem__(self, name):
        """Get a database by name.
        Raises :class:`~pymongo.errors.InvalidName` if an invalid
        database name is used.
        :Parameters:
          - `name`: the name of the database to get
        """
        return database.Database(self, name)

    def close_cursor(self, cursor_id, address=None):
        """DEPRECATED - Send a kill cursors message soon with the given id.
        Raises :class:`TypeError` if `cursor_id` is not an instance of
        ``(int, long)``. What closing the cursor actually means
        depends on this client's cursor manager.
        This method may be called from a :class:`~pymongo.cursor.Cursor`
        destructor during garbage collection, so it isn't safe to take a
        lock or do network I/O. Instead, we schedule the cursor to be closed
        soon on a background thread.
        :Parameters:
          - `cursor_id`: id of cursor to close
          - `address` (optional): (host, port) pair of the cursor's server.
            If it is not provided, the client attempts to close the cursor on
            the primary or standalone, or a mongos server.
        .. versionchanged:: 3.7
           Deprecated.
        .. versionchanged:: 3.0
           Added ``address`` parameter.
        """
        warnings.warn(
            "close_cursor is deprecated.",
            DeprecationWarning,
            stacklevel=2)
        if not isinstance(cursor_id, integer_types):
            raise TypeError("cursor_id must be an instance of (int, long)")

        self._close_cursor(cursor_id, address)

    def _close_cursor(self, cursor_id, address):
        """Send a kill cursors message with the given id.
        What closing the cursor actually means depends on this client's
        cursor manager. If there is none, the cursor is closed asynchronously
        on a background thread.
        """
        if self.__cursor_manager is not None:
            self.__cursor_manager.close(cursor_id, address)
        else:
            self.__kill_cursors_queue.append((address, [cursor_id]))

    def _close_cursor_now(self, cursor_id, address=None, session=None):
        """Send a kill cursors message with the given id.
        What closing the cursor actually means depends on this client's
        cursor manager. If there is none, the cursor is closed synchronously
        on the current thread.
        """
        if not isinstance(cursor_id, integer_types):
            raise TypeError("cursor_id must be an instance of (int, long)")

        if self.__cursor_manager is not None:
            self.__cursor_manager.close(cursor_id, address)
        else:
            try:
                self._kill_cursors(
                    [cursor_id], address, self._get_topology(), session)
            except PyMongoError:
                # Make another attempt to kill the cursor later.
                self.__kill_cursors_queue.append((address, [cursor_id]))

    def kill_cursors(self, cursor_ids, address=None):
        """DEPRECATED - Send a kill cursors message soon with the given ids.
        Raises :class:`TypeError` if `cursor_ids` is not an instance of
        ``list``.
        :Parameters:
          - `cursor_ids`: list of cursor ids to kill
          - `address` (optional): (host, port) pair of the cursor's server.
            If it is not provided, the client attempts to close the cursor on
            the primary or standalone, or a mongos server.
        .. versionchanged:: 3.3
           Deprecated.
        .. versionchanged:: 3.0
           Now accepts an `address` argument. Schedules the cursors to be
           closed on a background thread instead of sending the message
           immediately.
        """
        warnings.warn(
            "kill_cursors is deprecated.",
            DeprecationWarning,
            stacklevel=2)

        if not isinstance(cursor_ids, list):
            raise TypeError("cursor_ids must be a list")

        # "Atomic", needs no lock.
        self.__kill_cursors_queue.append((address, cursor_ids))

    def _kill_cursors(self, cursor_ids, address, topology, session):
        """Send a kill cursors message with the given ids."""
        listeners = self._event_listeners
        publish = listeners.enabled_for_commands
        if address:
            # address could be a tuple or _CursorAddress, but
            # select_server_by_address needs (host, port).
            server = topology.select_server_by_address(tuple(address))
        else:
            # Application called close_cursor() with no address.
            server = topology.select_server(writable_server_selector)

        try:
            namespace = address.namespace
            db, coll = namespace.split('.', 1)
        except AttributeError:
            namespace = None
            db = coll = "OP_KILL_CURSORS"

        spec = SON([('killCursors', coll), ('cursors', cursor_ids)])
        with server.get_socket(self.__all_credentials) as sock_info:
            if sock_info.max_wire_version >= 4 and namespace is not None:
                sock_info.command(db, spec, session=session, client=self)
            else:
                if publish:
                    start = datetime.datetime.now()
                request_id, msg = message.kill_cursors(cursor_ids)
                if publish:
                    duration = datetime.datetime.now() - start
                    # Here and below, address could be a tuple or
                    # _CursorAddress. We always want to publish a
                    # tuple to match the rest of the monitoring
                    # API.
                    listeners.publish_command_start(
                        spec, db, request_id, tuple(address))
                    start = datetime.datetime.now()

                try:
                    sock_info.send_message(msg, 0)
                except Exception as exc:
                    if publish:
                        dur = ((datetime.datetime.now() - start) + duration)
                        listeners.publish_command_failure(
                            dur, message._convert_exception(exc),
                            'killCursors', request_id,
                            tuple(address))
                    raise

                if publish:
                    duration = ((datetime.datetime.now() - start) + duration)
                    # OP_KILL_CURSORS returns no reply, fake one.
                    reply = {'cursorsUnknown': cursor_ids, 'ok': 1}
                    listeners.publish_command_success(
                        duration, reply, 'killCursors', request_id,
                        tuple(address))

    def _process_kill_cursors(self):
        """Process any pending kill cursors requests."""
        address_to_cursor_ids = defaultdict(list)

        # Other threads or the GC may append to the queue concurrently.
        while True:
            try:
                address, cursor_ids = self.__kill_cursors_queue.pop()
            except IndexError:
                break

            address_to_cursor_ids[address].extend(cursor_ids)

        # Don't re-open topology if it's closed and there's no pending cursors.
        if address_to_cursor_ids:
            topology = self._get_topology()
            for address, cursor_ids in address_to_cursor_ids.items():
                try:
                    self._kill_cursors(
                        cursor_ids, address, topology, session=None)
                except Exception:
                    helpers._handle_exception()

    # This method is run periodically by a background thread.
    def _process_periodic_tasks(self):
        """Process any pending kill cursors requests and
        maintain connection pool parameters."""
        self._process_kill_cursors()
        try:
            self._topology.update_pool(self.__all_credentials)
        except Exception:
            helpers._handle_exception()

    def __start_session(self, implicit, **kwargs):
        # Driver Sessions Spec: "If startSession is called when multiple users
        # are authenticated drivers MUST raise an error with the error message
        # 'Cannot call startSession when multiple users are authenticated.'"
        authset = set(self.__all_credentials.values())
        if len(authset) > 1:
            raise InvalidOperation("Cannot call start_session when"
                                   " multiple users are authenticated")

        # Raises ConfigurationError if sessions are not supported.
        server_session = self._get_server_session()
        opts = client_session.SessionOptions(**kwargs)
        return client_session.ClientSession(
            self, server_session, opts, authset, implicit)

    def start_session(self,
                      causal_consistency=True,
                      default_transaction_options=None):
        """Start a logical session.
        This method takes the same parameters as
        :class:`~pymongo.client_session.SessionOptions`. See the
        :mod:`~pymongo.client_session` module for details and examples.
        Requires MongoDB 3.6. It is an error to call :meth:`start_session`
        if this client has been authenticated to multiple databases using the
        deprecated method :meth:`~pymongo.database.Database.authenticate`.
        A :class:`~pymongo.client_session.ClientSession` may only be used with
        the MongoClient that started it.
        :Returns:
          An instance of :class:`~pymongo.client_session.ClientSession`.
        .. versionadded:: 3.6
        """
        return self.__start_session(
            False,
            causal_consistency=causal_consistency,
            default_transaction_options=default_transaction_options)

    def _get_server_session(self):
        """Internal: start or resume a _ServerSession."""
        return self._topology.get_server_session()

    def _return_server_session(self, server_session, lock):
        """Internal: return a _ServerSession to the pool."""
        return self._topology.return_server_session(server_session, lock)

    def _ensure_session(self, session=None):
        """If provided session is None, lend a temporary session."""
        if session:
            return session

        try:
            # Don't make implicit sessions causally consistent. Applications
            # should always opt-in.
            return self.__start_session(True, causal_consistency=False)
        except (ConfigurationError, InvalidOperation):
            # Sessions not supported, or multiple users authenticated.
            return None

    @contextlib.contextmanager
    def _tmp_session(self, session, close=True):
        """If provided session is None, lend a temporary session."""
        if session:
            # Don't call end_session.
            yield session
            return

        s = self._ensure_session(session)
        if s and close:
            with s:
                # Call end_session when we exit this scope.
                yield s
        elif s:
            try:
                # Only call end_session on error.
                yield s
            except Exception:
                s.end_session()
                raise
        else:
            yield None

    def _send_cluster_time(self, command, session):
        topology_time = self._topology.max_cluster_time()
        session_time = session.cluster_time if session else None
        if topology_time and session_time:
            if topology_time['clusterTime'] > session_time['clusterTime']:
                cluster_time = topology_time
            else:
                cluster_time = session_time
        else:
            cluster_time = topology_time or session_time
        if cluster_time:
            command['$clusterTime'] = cluster_time

    def _process_response(self, reply, session):
        self._topology.receive_cluster_time(reply.get('$clusterTime'))
        if session is not None:
            session._process_response(reply)

    def server_info(self, session=None):
        """Get information about the MongoDB server we're connected to.
        :Parameters:
          - `session` (optional): a
            :class:`~pymongo.client_session.ClientSession`.
        .. versionchanged:: 3.6
           Added ``session`` parameter.
        """
        return self.admin.command("buildinfo",
                                  read_preference=ReadPreference.PRIMARY,
                                  session=session)

    def list_databases(self, session=None, **kwargs):
        """Get a cursor over the databases of the connected server.
        :Parameters:
          - `session` (optional): a
            :class:`~pymongo.client_session.ClientSession`.
          - `**kwargs` (optional): Optional parameters of the
            `listDatabases command
            <https://docs.mongodb.com/manual/reference/command/listDatabases/>`_
            can be passed as keyword arguments to this method. The supported
            options differ by server version.
        :Returns:
          An instance of :class:`~pymongo.command_cursor.CommandCursor`.
        .. versionadded:: 3.6
        """
        cmd = SON([("listDatabases", 1)])
        cmd.update(kwargs)
        admin = self._database_default_options("admin")
        res = admin._retryable_read_command(cmd, session=session)
        # listDatabases doesn't return a cursor (yet). Fake one.
        cursor = {
            "id": 0,
            "firstBatch": res["databases"],
            "ns": "admin.$cmd",
        }
        return CommandCursor(admin["$cmd"], cursor, None)

    def list_database_names(self, session=None):
        """Get a list of the names of all databases on the connected server.
        :Parameters:
          - `session` (optional): a
            :class:`~pymongo.client_session.ClientSession`.
        .. versionadded:: 3.6
        """
        return [doc["name"]
                for doc in self.list_databases(session, nameOnly=True)]

    def database_names(self, session=None):
        """**DEPRECATED**: Get a list of the names of all databases on the
        connected server.
        :Parameters:
          - `session` (optional): a
            :class:`~pymongo.client_session.ClientSession`.
        .. versionchanged:: 3.7
           Deprecated. Use :meth:`list_database_names` instead.
        .. versionchanged:: 3.6
           Added ``session`` parameter.
        """
        warnings.warn("database_names is deprecated. Use list_database_names "
                      "instead.", DeprecationWarning, stacklevel=2)
        return self.list_database_names(session)

    def drop_database(self, name_or_database, session=None):
        """Drop a database.
        Raises :class:`TypeError` if `name_or_database` is not an instance of
        :class:`basestring` (:class:`str` in python 3) or
        :class:`~pymongo.database.Database`.
        :Parameters:
          - `name_or_database`: the name of a database to drop, or a
            :class:`~pymongo.database.Database` instance representing the
            database to drop
          - `session` (optional): a
            :class:`~pymongo.client_session.ClientSession`.
        .. versionchanged:: 3.6
           Added ``session`` parameter.
        .. note:: The :attr:`~pymongo.mongo_client.MongoClient.write_concern` of
           this client is automatically applied to this operation when using
           MongoDB >= 3.4.
        .. versionchanged:: 3.4
           Apply this client's write concern automatically to this operation
           when connected to MongoDB >= 3.4.
        """
        name = name_or_database
        if isinstance(name, database.Database):
            name = name.name

        if not isinstance(name, string_type):
            raise TypeError("name_or_database must be an instance "
                            "of %s or a Database" % (string_type.__name__,))

        self._purge_index(name)
        with self._socket_for_writes(session) as sock_info:
            self[name]._command(
                sock_info,
                "dropDatabase",
                read_preference=ReadPreference.PRIMARY,
                write_concern=self._write_concern_for(session),
                parse_write_concern_error=True,
                session=session)

    def get_default_database(self, default=None, codec_options=None,
            read_preference=None, write_concern=None, read_concern=None):
        """Get the database named in the MongoDB connection URI.
        >>> uri = 'mongodb://host/my_database'
        >>> client = MongoClient(uri)
        >>> db = client.get_default_database()
        >>> assert db.name == 'my_database'
        >>> db = client.get_database()
        >>> assert db.name == 'my_database'
        Useful in scripts where you want to choose which database to use
        based only on the URI in a configuration file.
        :Parameters:
          - `default` (optional): the database name to use if no database name
            was provided in the URI.
          - `codec_options` (optional): An instance of
            :class:`~bson.codec_options.CodecOptions`. If ``None`` (the
            default) the :attr:`codec_options` of this :class:`MongoClient` is
            used.
          - `read_preference` (optional): The read preference to use. If
            ``None`` (the default) the :attr:`read_preference` of this
            :class:`MongoClient` is used. See :mod:`~pymongo.read_preferences`
            for options.
          - `write_concern` (optional): An instance of
            :class:`~pymongo.write_concern.WriteConcern`. If ``None`` (the
            default) the :attr:`write_concern` of this :class:`MongoClient` is
            used.
          - `read_concern` (optional): An instance of
            :class:`~pymongo.read_concern.ReadConcern`. If ``None`` (the
            default) the :attr:`read_concern` of this :class:`MongoClient` is
            used.
        .. versionchanged:: 3.8
           Undeprecated. Added the ``default``, ``codec_options``,
           ``read_preference``, ``write_concern`` and ``read_concern``
           parameters.
        .. versionchanged:: 3.5
           Deprecated, use :meth:`get_database` instead.
        """
        if self.__default_database_name is None and default is None:
            raise ConfigurationError(
                'No default database name defined or provided.')

        return database.Database(
            self, self.__default_database_name or default, codec_options,
            read_preference, write_concern, read_concern)

    def get_database(self, name=None, codec_options=None, read_preference=None,
                     write_concern=None, read_concern=None):
        """Get a :class:`~pymongo.database.Database` with the given name and
        options.
        Useful for creating a :class:`~pymongo.database.Database` with
        different codec options, read preference, and/or write concern from
        this :class:`MongoClient`.
          >>> client.read_preference
          Primary()
          >>> db1 = client.test
          >>> db1.read_preference
          Primary()
          >>> from pymongo import ReadPreference
          >>> db2 = client.get_database(
          ...     'test', read_preference=ReadPreference.SECONDARY)
          >>> db2.read_preference
          Secondary(tag_sets=None)
        :Parameters:
          - `name` (optional): The name of the database - a string. If ``None``
            (the default) the database named in the MongoDB connection URI is
            returned.
          - `codec_options` (optional): An instance of
            :class:`~bson.codec_options.CodecOptions`. If ``None`` (the
            default) the :attr:`codec_options` of this :class:`MongoClient` is
            used.
          - `read_preference` (optional): The read preference to use. If
            ``None`` (the default) the :attr:`read_preference` of this
            :class:`MongoClient` is used. See :mod:`~pymongo.read_preferences`
            for options.
          - `write_concern` (optional): An instance of
            :class:`~pymongo.write_concern.WriteConcern`. If ``None`` (the
            default) the :attr:`write_concern` of this :class:`MongoClient` is
            used.
          - `read_concern` (optional): An instance of
            :class:`~pymongo.read_concern.ReadConcern`. If ``None`` (the
            default) the :attr:`read_concern` of this :class:`MongoClient` is
            used.
        .. versionchanged:: 3.5
           The `name` parameter is now optional, defaulting to the database
           named in the MongoDB connection URI.
        """
        if name is None:
            if self.__default_database_name is None:
                raise ConfigurationError('No default database defined')
            name = self.__default_database_name

        return database.Database(
            self, name, codec_options, read_preference,
            write_concern, read_concern)

    def _database_default_options(self, name):
        """Get a Database instance with the default settings."""
        return self.get_database(
            name, codec_options=DEFAULT_CODEC_OPTIONS,
            read_preference=ReadPreference.PRIMARY,
            write_concern=DEFAULT_WRITE_CONCERN)

    @property
    def is_locked(self):
        """Is this server locked? While locked, all write operations
        are blocked, although read operations may still be allowed.
        Use :meth:`unlock` to unlock.
        """
        ops = self._database_default_options('admin')._current_op()
        return bool(ops.get('fsyncLock', 0))

    def fsync(self, **kwargs):
        """Flush all pending writes to datafiles.
        Optional parameters can be passed as keyword arguments:
          - `lock`: If True lock the server to disallow writes.
          - `async`: If True don't block while synchronizing.
          - `session` (optional): a
            :class:`~pymongo.client_session.ClientSession`.
        .. note:: Starting with Python 3.7 `async` is a reserved keyword.
          The async option to the fsync command can be passed using a
          dictionary instead::
            options = {'async': True}
            client.fsync(**options)
        .. versionchanged:: 3.6
           Added ``session`` parameter.
        .. warning:: `async` and `lock` can not be used together.
        .. warning:: MongoDB does not support the `async` option
                     on Windows and will raise an exception on that
                     platform.
        """
        self.admin.command("fsync",
                           read_preference=ReadPreference.PRIMARY, **kwargs)

    def unlock(self, session=None):
        """Unlock a previously locked server.
        :Parameters:
          - `session` (optional): a
            :class:`~pymongo.client_session.ClientSession`.
        .. versionchanged:: 3.6
           Added ``session`` parameter.
        """
        cmd = SON([("fsyncUnlock", 1)])
        with self._socket_for_writes(session) as sock_info:
            if sock_info.max_wire_version >= 4:
                try:
                    with self._tmp_session(session) as s:
                        sock_info.command(
                            "admin", cmd, session=s, client=self)
                except OperationFailure as exc:
                    # Ignore "DB not locked" to replicate old behavior
                    if exc.code != 125:
                        raise
            else:
                message._first_batch(sock_info, "admin", "$cmd.sys.unlock",
                                     {}, -1, True, self.codec_options,
                                     ReadPreference.PRIMARY, cmd,
                                     self._event_listeners)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        raise TypeError("'MongoClient' object is not iterable")

    next = __next__


def _retryable_error_doc(exc):
    """Return the server response from PyMongo exception or None."""
    if isinstance(exc, BulkWriteError):
        # Check the last writeConcernError to determine if this
        # BulkWriteError is retryable.
        wces = exc.details['writeConcernErrors']
        wce = wces[-1] if wces else None
        return wce
    if isinstance(exc, (NotMasterError, OperationFailure)):
        return exc.details
    return None


def _retryable_writes_error(exc, max_wire_version):
    doc = _retryable_error_doc(exc)
    if doc:
        code = doc.get('code', 0)
        # retryWrites on MMAPv1 should raise an actionable error.
        if (code == 20 and
                str(exc).startswith("Transaction numbers")):
            errmsg = (
                "This MongoDB deployment does not support "
                "retryable writes. Please add retryWrites=false "
                "to your connection string.")
            raise OperationFailure(errmsg, code, exc.details)
        if max_wire_version >= 9:
            # MongoDB 4.4+ utilizes RetryableWriteError.
            return 'RetryableWriteError' in doc.get('errorLabels', [])
        else:
            if code in helpers._RETRYABLE_ERROR_CODES:
                exc._add_error_label("RetryableWriteError")
                return True
            return False

    if isinstance(exc, ConnectionFailure):
        exc._add_error_label("RetryableWriteError")
        return True
    return False


class _MongoClientErrorHandler(object):
    """Handle errors raised when executing an operation."""
    __slots__ = ('client', 'server_address', 'session', 'max_wire_version',
                 'sock_generation')

    def __init__(self, client, server, session):
        self.client = client
        self.server_address = server.description.address
        self.session = session
        self.max_wire_version = common.MIN_WIRE_VERSION
        # XXX: When get_socket fails, this generation could be out of date:
        # "Note that when a network error occurs before the handshake
        # completes then the error's generation number is the generation
        # of the pool at the time the connection attempt was started."
        self.sock_generation = server.pool.generation

    def contribute_socket(self, sock_info):
        """Provide socket information to the error handler."""
        self.max_wire_version = sock_info.max_wire_version
        self.sock_generation = sock_info.generation

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            return

        if self.session:
            if issubclass(exc_type, ConnectionFailure):
                if self.session.in_transaction:
                    exc_val._add_error_label("TransientTransactionError")
                self.session._server_session.mark_dirty()

            if issubclass(exc_type, PyMongoError):
                if exc_val.has_error_label("TransientTransactionError"):
                    self.session._unpin_mongos()

        err_ctx = _ErrorContext(
            exc_val, self.max_wire_version, self.sock_generation)
        self.client._topology.handle_error(self.server_address, err_ctx)