# -*- coding: utf-8 -*-
from __future__ import with_statement, absolute_import
import os
import re
import sys
import time
import functools
import warnings
import sqlalchemy
from functools import partial
from operator import itemgetter
from threading import Lock
from blinker import Namespace
from sqlalchemy import orm, event, inspect
from sqlalchemy.orm.exc import UnmappedClassError
from sqlalchemy.orm.session import Session as SessionBase
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.declarative import declarative_base, DeclarativeMeta
from werkzeug.local import LocalStack
from ._compat import string_types, itervalues

# the best timer function for the platform
if sys.platform == 'win32':
    _timer = time.clock
else:
    _timer = time.time

connection_stack = LocalStack()

_camelcase_re = re.compile(r'([A-Z]+)(?=[a-z0-9])')
_signals = Namespace()

models_committed = _signals.signal('models-committed')
before_models_committed = _signals.signal('before-models-committed')


def _calling_context(app_path):
    frm = sys._getframe(1)
    while frm.f_back is not None:
        name = frm.f_globals.get('__name__')
        if name and (name == app_path or name.startswith(app_path + '.')):
            funcname = frm.f_code.co_name
            return '%s:%s (%s)' % (
                frm.f_code.co_filename,
                frm.f_lineno,
                funcname
            )
        frm = frm.f_back
    return '<unknown>'


def _record_queries(app):
    if app.config['DEBUG']:
        return True
    rq = app.config['SQLALCHEMY_RECORD_QUERIES']
    if rq is not None:
        return rq
    return bool(app.config['TESTING'])


class _DebugQueryTuple(tuple):
    statement = property(itemgetter(0))
    parameters = property(itemgetter(1))
    start_time = property(itemgetter(2))
    end_time = property(itemgetter(3))
    context = property(itemgetter(4))

    @property
    def duration(self):
        return self.end_time - self.start_time

    def __repr__(self):
        return '<query statement="%s" parameters=%r duration=%.03f>' % (
            self.statement,
            self.parameters,
            self.duration
        )


def _should_set_tablename(bases, d):
    """Check what values are set by a class and its bases to determine if a
    tablename should be automatically generated.

    The class and its bases are checked in order of precedence: the class
    itself then each base in the order they were given at class definition.

    Abstract classes do not generate a tablename, although they may have set
    or inherited a tablename elsewhere.

    If a class defines a tablename or table, a new one will not be generated.
    Otherwise, if the class defines a primary key, a new name will be generated.

    This supports:

    * Joined table inheritance without explicitly naming sub-models.
    * Single table inheritance.
    * Inheriting from mixins or abstract models.

    :param bases: base classes of new class
    :param d: new class dict
    :return: True if tablename should be set
    """

    if '__tablename__' in d or '__table__' in d or '__abstract__' in d:
        return False

    if any(v.primary_key for v in itervalues(d) if
           isinstance(v, sqlalchemy.Column)):
        return True

    for base in bases:
        if hasattr(base, '__tablename__') or hasattr(base, '__table__'):
            return False

        for name in dir(base):
            attr = getattr(base, name)

            if isinstance(attr, sqlalchemy.Column) and attr.primary_key:
                return True


class _BoundDeclarativeMeta(DeclarativeMeta):
    def __new__(cls, name, bases, d):
        if _should_set_tablename(bases, d):
            def _join(match):
                word = match.group()
                if len(word) > 1:
                    return ('_%s_%s' % (word[:-1], word[-1])).lower()
                return '_' + word.lower()

            d['__tablename__'] = _camelcase_re.sub(_join, name).lstrip('_')

        return DeclarativeMeta.__new__(cls, name, bases, d)

    def __init__(self, name, bases, d):
        bind_key = d.pop('__bind_key__', None)
        DeclarativeMeta.__init__(self, name, bases, d)
        if bind_key is not None:
            self.__table__.info['bind_key'] = bind_key


class _EngineDebuggingSignalEvents(object):
    """Sets up handlers for two events that let us track the execution time of queries."""

    def __init__(self, engine, import_name):
        self.engine = engine
        self.app_package = import_name

    def register(self):
        event.listen(self.engine, 'before_cursor_execute',
                     self.before_cursor_execute)
        event.listen(self.engine, 'after_cursor_execute',
                     self.after_cursor_execute)

    def before_cursor_execute(self, conn, cursor, statement,
                              parameters, context, executemany):
        if connection_stack.top is not None:
            context._query_start_time = _timer()

    def after_cursor_execute(self, conn, cursor, statement,
                             parameters, context, executemany):
        ctx = connection_stack.top
        if ctx is not None:
            queries = getattr(ctx, 'sqlalchemy_queries', None)
            if queries is None:
                queries = []
                setattr(ctx, 'sqlalchemy_queries', queries)
            queries.append(_DebugQueryTuple((
                statement, parameters, context._query_start_time, _timer(),
                _calling_context(self.app_package))))


class _EngineConnector(object):
    def __init__(self, sa, app, bind=None):
        self._sa = sa
        self._app = app
        self._engine = None
        self._connected_for = None
        self._bind = bind
        self._lock = Lock()

    def get_uri(self):
        if self._bind is None:
            return self._app.config['SQLALCHEMY_DATABASE_URI']
        binds = self._app.config['SQLALCHEMY_BINDS'] or ()
        assert self._bind in binds, \
            'Bind %r is not specified.  Set it in the SQLALCHEMY_BINDS ' \
            'configuration variable' % self._bind
        return binds[self._bind]

    def get_engine(self):
        with self._lock:
            uri = self.get_uri()
            echo = self._app.config['SQLALCHEMY_ECHO']
            if (uri, echo) == self._connected_for:
                return self._engine
            info = make_url(uri)
            options = {'convert_unicode': True}
            self._sa.apply_pool_defaults(self._app, options)
            self._sa.apply_driver_hacks(self._app, info, options)
            if echo:
                options['echo'] = True
            self._engine = rv = sqlalchemy.create_engine(info, **options)
            if _record_queries(self._app):
                _EngineDebuggingSignalEvents(self._engine,
                                             self._app.import_name).register()
            self._connected_for = (uri, echo)
            return rv


class _QueryProperty(object):
    def __init__(self, sa):
        self.sa = sa

    def __get__(self, obj, type):
        try:
            mapper = orm.class_mapper(type)
            if mapper:
                return type.query_class(mapper, session=self.sa.session())
        except UnmappedClassError:
            return None


def _make_table(db):
    def _make_table(*args, **kwargs):
        if len(args) > 1 and isinstance(args[1], db.Column):
            args = (args[0], db.metadata) + args[1:]
        info = kwargs.pop('info', None) or {}
        info.setdefault('bind_key', None)
        kwargs['info'] = info
        return sqlalchemy.Table(*args, **kwargs)

    return _make_table


def _set_default_query_class(d):
    if 'query_class' not in d:
        d['query_class'] = orm.Query


def _wrap_with_default_query_class(fn):
    @functools.wraps(fn)
    def newfn(*args, **kwargs):
        _set_default_query_class(kwargs)
        if "backref" in kwargs:
            backref = kwargs['backref']
            if isinstance(backref, string_types):
                backref = (backref, {})
            _set_default_query_class(backref[1])
        return fn(*args, **kwargs)

    return newfn


def _include_sqlalchemy(obj):
    for module in sqlalchemy, sqlalchemy.orm:
        for key in module.__all__:
            if not hasattr(obj, key):
                setattr(obj, key, getattr(module, key))
    # Note: obj.Table does not attempt to be a SQLAlchemy Table class.
    obj.Table = _make_table(obj)
    obj.relationship = _wrap_with_default_query_class(obj.relationship)
    obj.relation = _wrap_with_default_query_class(obj.relation)
    obj.dynamic_loader = _wrap_with_default_query_class(obj.dynamic_loader)
    obj.event = event


class SignallingSession(SessionBase):
    """The signalling session is the default session that Flask-SQLAlchemy
    uses.  It extends the default session system with bind selection and
    modification tracking.

    If you want to use a different session you can override the
    :meth:`SQLAlchemy.create_session` function.

    .. versionadded:: 2.0

    .. versionadded:: 2.1
        The `binds` option was added, which allows a session to be joined
        to an external transaction.
    """

    def __init__(self, db, autocommit=False, autoflush=True, app=None,
                 **options):
        #: The application that this session belongs to.
        self.app = app = db.get_app()
        track_modifications = app.config['SQLALCHEMY_TRACK_MODIFICATIONS']
        bind = options.pop('bind', None) or db.engine
        binds = options.pop('binds', None) or db.get_binds(app)

        if track_modifications is None or track_modifications:
            _SessionSignalEvents.register(self)

        SessionBase.__init__(
            self, autocommit=autocommit, autoflush=autoflush,
            bind=bind, binds=binds, **options
        )

    def get_bind(self, mapper=None, clause=None):
        # mapper is None if someone tries to just get a connection
        if mapper is not None:
            info = getattr(mapper.mapped_table, 'info', {})
            bind_key = info.get('bind_key')
            if bind_key is not None:
                state = get_state(self.app)
                return state.db.get_engine(self.app, bind=bind_key)
        return SessionBase.get_bind(self, mapper, clause)


def get_state(app):
    """Gets the state for the application"""
    assert 'sqlalchemy' in app.extensions, \
        'The sqlalchemy extension was not registered to the current ' \
        'application.  Please make sure to call init_app() first.'
    return app.extensions['sqlalchemy']


class Model(object):
    """Baseclass for custom user models."""

    #: the query class used.  The :attr:`query` attribute is an instance
    #: of this class.  By default a :class:`BaseQuery` is used.
    query_class = orm.Query

    #: an instance of :attr:`query_class`.  Can be used to query the
    #: database for instances of this model.
    query = None


class _SessionSignalEvents(object):
    @classmethod
    def register(cls, session):
        if not hasattr(session, '_model_changes'):
            session._model_changes = {}

        event.listen(session, 'before_flush', cls.record_ops)
        event.listen(session, 'before_commit', cls.record_ops)
        event.listen(session, 'before_commit', cls.before_commit)
        event.listen(session, 'after_commit', cls.after_commit)
        event.listen(session, 'after_rollback', cls.after_rollback)

    @classmethod
    def unregister(cls, session):
        if hasattr(session, '_model_changes'):
            del session._model_changes

        event.remove(session, 'before_flush', cls.record_ops)
        event.remove(session, 'before_commit', cls.record_ops)
        event.remove(session, 'before_commit', cls.before_commit)
        event.remove(session, 'after_commit', cls.after_commit)
        event.remove(session, 'after_rollback', cls.after_rollback)

    @staticmethod
    def record_ops(session, flush_context=None, instances=None):
        try:
            d = session._model_changes
        except AttributeError:
            return

        for targets, operation in (
                (session.new, 'insert'), (session.dirty, 'update'),
                (session.deleted, 'delete')):
            for target in targets:
                state = inspect(target)
                key = state.identity_key if state.has_identity else id(target)
                d[key] = (target, operation)

    @staticmethod
    def before_commit(session):
        try:
            d = session._model_changes
        except AttributeError:
            return

        if d:
            before_models_committed.send(session.app, changes=list(d.values()))

    @staticmethod
    def after_commit(session):
        try:
            d = session._model_changes
        except AttributeError:
            return

        if d:
            models_committed.send(session.app, changes=list(d.values()))
            d.clear()

    @staticmethod
    def after_rollback(session):
        try:
            d = session._model_changes
        except AttributeError:
            return

        d.clear()


class _SQLAlchemyState(object):
    """Remembers configuration for the (db, app) tuple."""

    def __init__(self, db, app):
        self.db = db
        self.app = app
        self.connectors = {}


class SQLAlchemy(object):
    def __init__(self, app=None, use_native_unicode=True, session_options=None,
                 metadata=None):

        if session_options is None:
            session_options = {}

        session_options.setdefault('scopefunc', connection_stack.__ident_func__)
        self.use_native_unicode = use_native_unicode
        self.session = self.create_scoped_session(session_options)
        self.Model = self.make_declarative_base(metadata)
        self.Query = orm.Query
        self._engine_lock = Lock()
        self.app = app
        _include_sqlalchemy(self)

        if app is not None:
            self.init_app(app)

    @property
    def metadata(self):
        """Returns the metadata"""
        return self.Model.metadata

    def create_scoped_session(self, options=None):
        """Helper factory method that creates a scoped session.  It
        internally calls :meth:`create_session`.
        """
        if options is None:
            options = {}
        scopefunc = options.pop('scopefunc', None)
        return orm.scoped_session(partial(self.create_session, options),
                                  scopefunc=scopefunc)

    def create_session(self, options):
        """Creates the session.  The default implementation returns a
        :class:`SignallingSession`.

        .. versionadded:: 2.0
        """
        return SignallingSession(self, **options)

    def make_declarative_base(self, metadata=None):
        """Creates the declarative base."""
        base = declarative_base(cls=Model, name='Model',
                                metadata=metadata,
                                metaclass=_BoundDeclarativeMeta)
        base.query = _QueryProperty(self)
        return base

    def init_app(self, app):
        """This callback can be used to initialize an application for the
        use with this database setup.  Never use a database in the context
        of an application not initialized that way or connections will
        leak.
        """
        app.config.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite://')
        app.config.setdefault('SQLALCHEMY_BINDS', None)
        app.config.setdefault('SQLALCHEMY_NATIVE_UNICODE', None)
        app.config.setdefault('SQLALCHEMY_ECHO', False)
        app.config.setdefault('SQLALCHEMY_RECORD_QUERIES', None)
        app.config.setdefault('SQLALCHEMY_POOL_SIZE', None)
        app.config.setdefault('SQLALCHEMY_POOL_TIMEOUT', None)
        app.config.setdefault('SQLALCHEMY_POOL_RECYCLE', None)
        app.config.setdefault('SQLALCHEMY_MAX_OVERFLOW', None)
        app.config.setdefault('SQLALCHEMY_COMMIT_ON_TEARDOWN', False)
        track_modifications = app.config.setdefault(
            'SQLALCHEMY_TRACK_MODIFICATIONS', None)

        if track_modifications is None:
            warnings.warn(
                'SQLALCHEMY_TRACK_MODIFICATIONS adds significant overhead and will be disabled by default in the future.  Set it to True to suppress this warning.')

        if not hasattr(app, 'extensions'):
            app.extensions = {}
        app.extensions['sqlalchemy'] = _SQLAlchemyState(self, app)

        # 0.9 and later
        if hasattr(app, 'teardown_appcontext'):
            teardown = app.teardown_appcontext
        # 0.7 to 0.8
        elif hasattr(app, 'teardown_request'):
            teardown = app.teardown_request
        # Older Flask versions
        else:
            if app.config['SQLALCHEMY_COMMIT_ON_TEARDOWN']:
                raise RuntimeError("Commit on teardown requires Flask >= 0.7")
            teardown = app.after_request

        @teardown
        def shutdown_session(response_or_exc):
            if app.config['SQLALCHEMY_COMMIT_ON_TEARDOWN']:
                if response_or_exc is None:
                    self.session.commit()
            self.session.remove()
            return response_or_exc

    def apply_pool_defaults(self, app, options):
        def _setdefault(optionkey, configkey):
            value = app.config[configkey]
            if value is not None:
                options[optionkey] = value

        _setdefault('pool_size', 'SQLALCHEMY_POOL_SIZE')
        _setdefault('pool_timeout', 'SQLALCHEMY_POOL_TIMEOUT')
        _setdefault('pool_recycle', 'SQLALCHEMY_POOL_RECYCLE')
        _setdefault('max_overflow', 'SQLALCHEMY_MAX_OVERFLOW')

    def apply_driver_hacks(self, app, info, options):
        """This method is called before engine creation and used to inject
        driver specific hacks into the options.  The `options` parameter is
        a dictionary of keyword arguments that will then be used to call
        the :func:`sqlalchemy.create_engine` function.

        The default implementation provides some saner defaults for things
        like pool sizes for MySQL and sqlite.  Also it injects the setting of
        `SQLALCHEMY_NATIVE_UNICODE`.
        """
        if info.drivername.startswith('mysql'):
            info.query.setdefault('charset', 'utf8')
            if info.drivername != 'mysql+gaerdbms':
                options.setdefault('pool_size', 10)
                options.setdefault('pool_recycle', 7200)
        elif info.drivername == 'sqlite':
            pool_size = options.get('pool_size')
            detected_in_memory = False
            # we go to memory and the pool size was explicitly set to 0
            # which is fail.  Let the user know that
            if info.database in (None, '', ':memory:'):
                detected_in_memory = True
                from sqlalchemy.pool import StaticPool

                options['poolclass'] = StaticPool
                if 'connect_args' not in options:
                    options['connect_args'] = {}
                options['connect_args']['check_same_thread'] = False

                if pool_size == 0:
                    raise RuntimeError('SQLite in memory database with an '
                                       'empty queue not possible due to data '
                                       'loss.')
            # if pool size is None or explicitly set to 0 we assume the
            # user did not want a queue for this sqlite connection and
            # hook in the null pool.
            elif not pool_size:
                from sqlalchemy.pool import NullPool

                options['poolclass'] = NullPool

            # if it's not an in memory database we make the path absolute.
            if not detected_in_memory:
                info.database = os.path.join(app.root_path, info.database)

        unu = app.config['SQLALCHEMY_NATIVE_UNICODE']
        if unu is None:
            unu = self.use_native_unicode
        if not unu:
            options['use_native_unicode'] = False

    @property
    def engine(self):
        """Gives access to the engine.  If the database configuration is bound
        to a specific application (initialized with an application) this will
        always return a database connection.  If however the current application
        is used this might raise a :exc:`RuntimeError` if no application is
        active at the moment.
        """
        return self.get_engine(self.get_app())

    def make_connector(self, app, bind=None):
        """Creates the connector for a given state and bind."""
        return _EngineConnector(self, app, bind)

    def get_engine(self, app, bind=None):
        """Returns a specific engine.

        .. versionadded:: 0.12
        """
        with self._engine_lock:
            state = get_state(app)
            connector = state.connectors.get(bind)
            if connector is None:
                connector = self.make_connector(app, bind)
                state.connectors[bind] = connector
            return connector.get_engine()

    def get_app(self, reference_app=None):
        """Helper method that implements the logic to look up an application.
        """
        if reference_app is not None:
            return reference_app
        if self.app is not None:
            return self.app
        ctx = connection_stack.top
        if ctx is not None:
            return ctx.app
        raise RuntimeError('application not registered on db '
                           'instance and no application bound '
                           'to current context')

    def get_tables_for_bind(self, bind=None):
        """Returns a list of all tables relevant for a bind."""
        result = []
        for table in itervalues(self.Model.metadata.tables):
            if table.info.get('bind_key') == bind:
                result.append(table)
        return result

    def get_binds(self, app=None):
        """Returns a dictionary with a table->engine mapping.

        This is suitable for use of sessionmaker(binds=db.get_binds(app)).
        """
        app = self.get_app(app)
        binds = [None] + list(app.config['SQLALCHEMY_BINDS'] or ())
        retval = {}
        for bind in binds:
            engine = self.get_engine(app, bind)
            tables = self.get_tables_for_bind(bind)
            retval.update(dict((table, engine) for table in tables))
        return retval

    def _execute_for_all_tables(self, app, bind, operation, skip_tables=False):
        app = self.get_app(app)

        if bind == '__all__':
            binds = [None] + list(app.config['SQLALCHEMY_BINDS'] or ())
        elif isinstance(bind, string_types) or bind is None:
            binds = [bind]
        else:
            binds = bind

        for bind in binds:
            extra = {}
            if not skip_tables:
                tables = self.get_tables_for_bind(bind)
                extra['tables'] = tables
            op = getattr(self.Model.metadata, operation)
            op(bind=self.get_engine(app, bind), **extra)

    def create_all(self, bind='__all__', app=None):
        """Creates all tables.

        .. versionchanged:: 0.12
           Parameters were added
        """
        self._execute_for_all_tables(app, bind, 'create_all')

    def drop_all(self, bind='__all__', app=None):
        """Drops all tables.

        .. versionchanged:: 0.12
           Parameters were added
        """
        self._execute_for_all_tables(app, bind, 'drop_all')

    def reflect(self, bind='__all__', app=None):
        """Reflects tables from the database.

        .. versionchanged:: 0.12
           Parameters were added
        """
        self._execute_for_all_tables(app, bind, 'reflect', skip_tables=True)

    def __repr__(self):
        app = None
        if self.app is not None:
            app = self.app
        else:
            ctx = connection_stack.top
            if ctx is not None:
                app = ctx.app
        return '<%s engine=%r>' % (
            self.__class__.__name__,
            app and app.config['SQLALCHEMY_DATABASE_URI'] or None
        )