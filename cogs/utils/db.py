# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2017 Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

# These are just things that allow me to make tables for PostgreSQL easier
# This isn't exactly good. It's just good enough for my uses.
# Also shoddy migration support.

from collections import OrderedDict
from pathlib import Path
import json
import os
import pydoc
import uuid
import datetime
import inspect
import decimal
import asyncpg
import logging
import asyncio

log = logging.getLogger(__name__)

class SchemaError(Exception):
    pass

class SQLType:
    python = None

    def to_dict(self):
        o = self.__dict__.copy()
        cls = self.__class__
        o['__meta__'] = cls.__module__ + '.' + cls.__qualname__
        return o

    @classmethod
    def from_dict(cls, data):
        meta = data.pop('__meta__')
        given = cls.__module__ + '.' + cls.__qualname__
        if given != meta:
            cls = pydoc.locate(meta)
            if cls is None:
                raise RuntimeError('Could not locate "%s".' % meta)

        self = cls.__new__(cls)
        self.__dict__.update(data)
        return self

    def __eq__(self, other):
        return isinstance(other, self.__class__) and self.__dict__ == other.__dict__

    def __ne__(self, other):
        return not self.__eq__(other)

    def to_sql(self):
        raise NotImplementedError()

    def is_real_type(self):
        return True

class Binary(SQLType):
    python = bytes

    def to_sql(self):
        return 'BYTEA'

class Boolean(SQLType):
    python = bool

    def to_sql(self):
        return 'BOOLEAN'

class Date(SQLType):
    python = datetime.date

    def to_sql(self):
        return 'DATE'

class Datetime(SQLType):
    python = datetime.datetime

    def __init__(self, *, timezone=False):
        self.timezone = timezone

    def to_sql(self):
        if self.timezone:
            return 'TIMESTAMP WITH TIMEZONE'
        return 'TIMESTAMP'

class Double(SQLType):
    python = float

    def to_sql(self):
        return 'REAL'

class Float(SQLType):
    python = float

    def to_sql(self):
        return 'FLOAT'

class Integer(SQLType):
    python = int

    def __init__(self, *, big=False, small=False, auto_increment=False):
        self.big = big
        self.small = small
        self.auto_increment = auto_increment

        if big and small:
            raise SchemaError('Integer column type cannot be both big and small.')

    def to_sql(self):
        if self.auto_increment:
            if self.big:
                return 'BIGSERIAL'
            if self.small:
                return 'SMALLSERIAL'
            return 'SERIAL'
        if self.big:
            return 'BIGINT'
        if self.small:
            return 'SMALLINT'
        return 'INTEGER'

    def is_real_type(self):
        return not self.auto_increment

class Interval(SQLType):
    python = datetime.timedelta

    def __init__(self, field=None):
        if field:
            field = field.upper()
            if field not in ('YEAR', 'MONTH', 'DAY', 'HOUR', 'MINUTE', 'SECOND',
                             'YEAR TO MONTH', 'DAY TO HOUR', 'DAY TO MINUTE', 'DAY TO SECOND',
                             'HOUR TO MINUTE', 'HOUR TO SECOND', 'MINUTE TO SECOND'):
                raise SchemaError('invalid interval specified')
            self.field = field
        else:
            self.field = None

    def to_sql(self):
        if self.field:
            return 'INTERVAL ' + self.field
        return 'INTERVAL'

class Numeric(SQLType):
    python = decimal.Decimal

    def __init__(self, *, precision=None, scale=None):
        if precision is not None:
            if precision < 0 or precision > 1000:
                raise SchemaError('precision must be greater than 0 and below 1000')
            if scale is None:
                scale = 0

        self.precision = precision
        self.scale = scale

    def to_sql(self):
        if self.precision is not None:
            return 'NUMERIC({0.precision}, {0.scale})'.format(self)
        return 'NUMERIC'

class String(SQLType):
    python = str

    def __init__(self, *, length=None, fixed=False):
        self.length = length
        self.fixed = fixed

        if fixed and length is None:
            raise SchemaError('Cannot have fixed string with no length')

    def to_sql(self):
        if self.length is None:
            return 'TEXT'
        if self.fixed:
            return 'CHAR({0.length})'.format(self)
        return 'VARCHAR({0.length})'.format(self)

class Time(SQLType):
    python = datetime.time

    def __init__(self, *, timezone=False):
        self.timezone = timezone

    def to_sql(self):
        if self.timezone:
            return 'TIME WITH TIME ZONE'
        return 'TIME'

class JSON(SQLType):
    python = None

    def to_sql(self):
        return 'JSONB'

class ForeignKey(SQLType):
    def __init__(self, table, column, *, sql_type=None, on_delete='CASCADE', on_update='NO ACTION'):
        if not table or not isinstance(table, str):
            raise SchemaError('missing table to reference (must be string)')

        valid_actions = (
            'NO ACTION',
            'RESTRICT',
            'CASCADE',
            'SET NULL',
            'SET DEFAULT',
        )

        on_delete = on_delete.upper()
        on_update = on_update.upper()

        if on_delete not in valid_actions:
            raise TypeError('on_delete must be one of %s.' % valid_actions)

        if on_update not in valid_actions:
            raise TypeError('on_update must be one of %s.' % valid_actions)


        self.table = table
        self.column = column
        self.on_update = on_update
        self.on_delete = on_delete

        if sql_type is None:
            sql_type = Integer

        if inspect.isclass(sql_type):
            sql_type = sql_type()

        if not isinstance(sql_type, SQLType):
            raise TypeError('Cannot have non-SQLType derived sql_type')

        if not sql_type.is_real_type():
            raise SchemaError('sql_type must be a "real" type')

        self.sql_type = sql_type.to_sql()

    def is_real_type(self):
        return False

    def to_sql(self):
        fmt = '{0.sql_type} REFERENCES {0.table} ({0.column})' \
              ' ON DELETE {0.on_delete} ON UPDATE {0.on_update}'
        return fmt.format(self)

class Array(SQLType):
    python = list

    def __init__(self, sql_type):
        if inspect.isclass(sql_type):
            sql_type = sql_type()

        if not isinstance(sql_type, SQLType):
            raise TypeError('Cannot have non-SQLType derived sql_type')

        if not sql_type.is_real_type():
            raise SchemaError('sql_type must be a "real" type')

        self.sql_type = sql_type.to_sql()

    def to_sql(self):
        return '{0.sql_type} ARRAY'.format(self)

    def is_real_type(self):
        # technically, it is a real type
        # however, it doesn't play very well with migrations
        # so we're going to pretend that it isn't
        return False

class Column:
    __slots__ = ( 'column_type', 'index', 'primary_key', 'nullable',
                  'default', 'unique', 'name', 'index_name' )
    def __init__(self, column_type, *, index=False, primary_key=False,
                 nullable=True, unique=False, default=None, name=None):

        if inspect.isclass(column_type):
            column_type = column_type()

        if not isinstance(column_type, SQLType):
            raise TypeError('Cannot have a non-SQLType derived column_type')

        self.column_type = column_type
        self.index = index
        self.unique = unique
        self.primary_key = primary_key
        self.nullable = nullable
        self.default = default
        self.name = name
        self.index_name = None # to be filled later

        if sum(map(bool, (unique, primary_key, default is not None))) > 1:
            raise SchemaError("'unique', 'primary_key', and 'default' are mutually exclusive.")

    @classmethod
    def from_dict(cls, data):
        index_name = data.pop('index_name', None)
        column_type = data.pop('column_type')
        column_type = SQLType.from_dict(column_type)
        self = cls(column_type=column_type, **data)
        self.index_name = index_name
        return self

    @property
    def _comparable_id(self):
        return '-'.join('%s:%s' % (attr, getattr(self, attr)) for attr in self.__slots__)

    def _to_dict(self):
        d = {
            attr: getattr(self, attr)
            for attr in self.__slots__
        }
        d['column_type'] = self.column_type.to_dict()
        return d

    def _qualifiers_dict(self):
        return { attr: getattr(self, attr) for attr in ('nullable', 'default')}

    def _is_rename(self, other):
        if self.name == other.name:
            return False

        return self.unique == other.unique and self.primary_key == other.primary_key

    def _create_table(self):
        builder = []
        builder.append(self.name)
        builder.append(self.column_type.to_sql())

        default = self.default
        if default is not None:
            builder.append('DEFAULT')
            if isinstance(default, str) and isinstance(self.column_type, String):
                builder.append("'%s'" % default)
            elif isinstance(default, bool):
                builder.append(str(default).upper())
            else:
                builder.append("(%s)" % default)
        elif self.unique:
            builder.append('UNIQUE')
        elif self.primary_key:
            builder.append('PRIMARY KEY')

        if not self.nullable:
            builder.append('NOT NULL')

        return ' '.join(builder)

class PrimaryKeyColumn(Column):
    """Shortcut for a SERIAL PRIMARY KEY column."""

    def __init__(self):
        super().__init__(Integer(auto_increment=True), primary_key=True)

class SchemaDiff:
    __slots__ = ('table', 'upgrade', 'downgrade')

    def __init__(self, table, upgrade, downgrade):
        self.table = table
        self.upgrade = upgrade
        self.downgrade = downgrade

    def to_dict(self):
        return { 'upgrade': self.upgrade, 'downgrade': self.downgrade }

    def is_empty(self):
        return len(self.upgrade) == 0 and len(self.downgrade) == 0

    def to_sql(self, *, downgrade=False):
        statements = []
        base = 'ALTER TABLE %s ' % self.table.__tablename__
        path = self.upgrade if not downgrade else self.downgrade

        for rename in path.get('rename_columns', []):
            fmt = '{0}RENAME COLUMN {1[before]} TO {1[after]};'.format(base, rename)
            statements.append(fmt)

        sub_statements = []
        for dropped in path.get('remove_columns', []):
            fmt = 'DROP COLUMN {0[name]} RESTRICT'.format(dropped)
            sub_statements.append(fmt)

        for changed_types in path.get('changed_column_types', []):
            fmt = 'ALTER COLUMN {0[name]} SET DATA TYPE {0[type]}'.format(changed_types)

            using = changed_types.get('using')
            if using is not None:
                fmt = '%s USING %s' % (fmt, using)

            sub_statements.append(fmt)

        for constraints in path.get('changed_constraints', []):
            before, after = constraints['before'], constraints['after']

            before_default, after_default = before.get('default'), after.get('default')
            if before_default is None and after_default is not None:
                fmt = 'ALTER COLUMN {0[name]} SET DEFAULT {1[default]}'.format(constraints, after)
                sub_statements.append(fmt)
            elif before_default is not None and after_default is None:
                fmt = 'ALTER COLUMN {0[name]} DROP DEFAULT'.format(constraints)
                sub_statements.append(fmt)

            before_nullable, after_nullable = before.get('nullable'), after.get('nullable')
            if not before_nullable and after_nullable:
                fmt = 'ALTER COLUMN {0[name]} DROP NOT NULL'.format(constraints)
                sub_statements.append(fmt)
            elif before_nullable and not after_nullable:
                fmt = 'ALTER COLUMN {0[name]} SET NOT NULL'.format(constraints)
                sub_statements.append(fmt)

        for added in path.get('add_columns', []):
            column = Column.from_dict(added)
            sub_statements.append('ADD COLUMN ' + column._create_table())

        if sub_statements:
            statements.append(base + ', '.join(sub_statements) + ';')

        # handle the index creation bits
        for dropped in path.get('drop_index', []):
            statements.append('DROP INDEX IF EXISTS {0[index]};'.format(dropped))

        for added in path.get('add_index', []):
            fmt = 'CREATE INDEX IF NOT EXISTS {0[index]} ON {1.__tablename__} ({0[name]});'
            statements.append(fmt.format(added, self.table))

        return '\n'.join(statements)

class MaybeAcquire:
    def __init__(self, connection, *, pool):
        self.connection = connection
        self.pool = pool
        self._cleanup = False

    async def __aenter__(self):
        if self.connection is None:
            self._cleanup = True
            self._connection = c = await self.pool.acquire()
            return c
        return self.connection

    async def __aexit__(self, *args):
        if self._cleanup:
            await self.pool.release(self._connection)

class TableMeta(type):
    @classmethod
    def __prepare__(cls, name, bases, **kwargs):
        return OrderedDict()

    def __new__(cls, name, parents, dct, **kwargs):
        columns = []

        try:
            table_name = kwargs['table_name']
        except KeyError:
            table_name = name.lower()

        dct['__tablename__'] = table_name

        for elem, value in dct.items():
            if isinstance(value, Column):
                if value.name is None:
                    value.name = elem

                if value.index:
                    value.index_name = '%s_%s_idx' % (table_name, value.name)

                columns.append(value)

        dct['columns'] = columns
        return super().__new__(cls, name, parents, dct)

    def __init__(self, name, parents, dct, **kwargs):
        super().__init__(name, parents, dct)

class Table(metaclass=TableMeta):
    @classmethod
    async def create_pool(cls, uri, **kwargs):
        """Sets up and returns the PostgreSQL connection pool that is used.

        .. note::

            This must be called at least once before doing anything with the tables.
            And must be called on the ``Table`` class.

        Parameters
        -----------
        uri: str
            The PostgreSQL URI to connect to.
        \*\*kwargs
            The arguments to forward to asyncpg.create_pool.
        """

        def _encode_jsonb(value):
            return json.dumps(value)

        def _decode_jsonb(value):
            return json.loads(value)

        old_init = kwargs.pop('init', None)

        async def init(con):
            await con.set_type_codec('jsonb', schema='pg_catalog', encoder=_encode_jsonb, decoder=_decode_jsonb, format='text')
            if old_init is not None:
                await old_init(con)

        cls._pool = pool = await asyncpg.create_pool(uri, init=init, **kwargs)
        return pool

    @classmethod
    def acquire_connection(cls, connection):
        return MaybeAcquire(connection, pool=cls._pool)

    @classmethod
    def write_migration(cls, *, directory='migrations'):
        """Writes the migration diff into the data file.

        Note
        ------
        This doesn't actually commit/do the migration.
        To do so, use :meth:`migrate`.

        Returns
        --------
        bool
            ``True`` if a migration was written, ``False`` otherwise.

        Raises
        -------
        RuntimeError
            Could not find the migration data necessary.
        """

        directory = Path(directory) / cls.__tablename__
        p = directory.with_suffix('.json')

        if not p.exists():
            raise RuntimeError('Could not find migration file.')

        current = directory.with_name('current-' + p.name)

        if not current.exists():
            raise RuntimeError('Could not find current data file.')

        with current.open() as fp:
            current_table = cls.from_dict(json.load(fp))

        diff = cls().diff(current_table)

        # the most common case, no difference
        if diff.is_empty():
            return None

        # load the migration data
        with p.open('r', encoding='utf-8') as fp:
            data = json.load(fp)
            migrations = data['migrations']

        # check if we should add it
        our_migrations = diff.to_dict()
        if len(migrations) == 0 or migrations[-1] != our_migrations:
            # we have a new migration, so add it
            migrations.append(our_migrations)
            temp_file = p.with_name('%s-%s.tmp' % (uuid.uuid4(), p.name))
            with temp_file.open('w', encoding='utf-8') as tmp:
                json.dump(data, tmp, ensure_ascii=True, indent=4)

            temp_file.replace(p)
            return True
        return False

    @classmethod
    async def migrate(cls, *, directory='migrations', index=-1, downgrade=False, verbose=False, connection=None):
        """Actually run the latest migration pointed by the data file.

        Parameters
        -----------
        directory: str
            The directory of where the migration data file resides.
        index: int
            The index of the migration array to use.
        downgrade: bool
            Whether to run an upgrade or a downgrade.
        verbose: bool
            Whether to output some information to stdout.
        connection: Optional[asyncpg.Connection]
            The connection to use, if not provided will acquire one from
            the internal pool.
        """

        directory = Path(directory) / cls.__tablename__
        p = directory.with_suffix('.json')
        if not p.exists():
            raise RuntimeError('Could not find migration file.')

        with p.open('r', encoding='utf-8') as fp:
            data = json.load(fp)
            migrations = data['migrations']

        try:
            migration = migrations[index]
        except IndexError:
            return False

        diff = SchemaDiff(cls, migration['upgrade'], migration['downgrade'])
        if diff.is_empty():
            return False

        async with MaybeAcquire(connection, pool=cls._pool) as con:
            sql = diff.to_sql(downgrade=downgrade)
            if verbose:
                print(sql)
            await con.execute(sql)

        current = directory.with_name('current-' + p.name)
        with current.open('w', encoding='utf-8') as fp:
            json.dump(cls.to_dict(), fp, indent=4, ensure_ascii=True)

    @classmethod
    async def create(cls, *, directory='migrations', verbose=False, connection=None, run_migrations=True):
        """Creates the database and manages migrations, if any.

        Parameters
        -----------
        directory: str
            The migrations directory.
        verbose: bool
            Whether to output some information to stdout.
        connection: Optional[asyncpg.Connection]
            The connection to use, if not provided will acquire one from
            the internal pool.
        run_migrations: bool
            Whether to run migrations at all.

        Returns
        --------
        Optional[bool]
            ``True`` if the table was successfully created or
            ``False`` if the table was successfully migrated or
            ``None`` if no migration took place.
        """
        directory = Path(directory) / cls.__tablename__
        p = directory.with_suffix('.json')
        current = directory.with_name('current-' + p.name)

        table_data = cls.to_dict()

        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)

            # we're creating this table for the first time,
            # it's an uncommon case so let's get it out of the way
            # first, try to actually create the table
            async with MaybeAcquire(connection, pool=cls._pool) as con:
                sql = cls.create_table(exists_ok=True)
                if verbose:
                    print(sql)
                await con.execute(sql)

            # since that step passed, let's go ahead and make the migration
            with p.open('w', encoding='utf-8') as fp:
                data = { 'table': table_data, 'migrations': [] }
                json.dump(data, fp, indent=4, ensure_ascii=True)

            with current.open('w', encoding='utf-8') as fp:
                json.dump(table_data, fp, indent=4, ensure_ascii=True)

            return True

        if not run_migrations:
            return None

        with current.open() as fp:
            current_table = cls.from_dict(json.load(fp))

        diff = cls().diff(current_table)

        # the most common case, no difference
        if diff.is_empty():
            return None

        # execute the upgrade SQL
        async with MaybeAcquire(connection, pool=cls._pool) as con:
            sql = diff.to_sql()
            if verbose:
                print(sql)
            await con.execute(sql)

        # load the migration data
        with p.open('r', encoding='utf-8') as fp:
            data = json.load(fp)
            migrations = data['migrations']

        # check if we should add it
        our_migrations = diff.to_dict()
        if len(migrations) == 0 or migrations[-1] != our_migrations:
            # we have a new migration, so add it
            migrations.append(our_migrations)
            temp_file = p.with_name('%s-%s.tmp' % (uuid.uuid4(), p.name))
            with temp_file.open('w', encoding='utf-8') as tmp:
                json.dump(data, tmp, ensure_ascii=True, indent=4)

            temp_file.replace(p)

        # update our "current" data in the filesystem
        with current.open('w', encoding='utf-8') as fp:
            json.dump(table_data, fp, indent=4, ensure_ascii=True)

        return False

    @classmethod
    async def drop(cls, *, directory='migrations', verbose=False, connection=None):
        """Drops the database and migrations, if any.

        Parameters
        -----------
        directory: str
            The migrations directory.
        verbose: bool
            Whether to output some information to stdout.
        connection: Optional[asyncpg.Connection]
            The connection to use, if not provided will acquire one from
            the internal pool.
        """

        directory = Path(directory) / cls.__tablename__
        p = directory.with_suffix('.json')
        current = directory.with_name('current-' + p.name)

        if not p.exists() or not current.exists():
            raise RuntimeError('Could not find the appropriate data files.')

        try:
            p.unlink()
        except:
            raise RuntimeError('Could not delete migration file')

        try:
            current.unlink()
        except:
            raise RuntimeError('Could not delete current migration file')

        async with MaybeAcquire(connection, pool=cls._pool) as con:
            sql = 'DROP TABLE {0} CASCADE;'.format(cls.__tablename__)
            if verbose:
                print(sql)
            await con.execute(sql)

    @classmethod
    def create_table(cls, *, exists_ok=True):
        """Generates the CREATE TABLE stub."""
        statements = []
        builder = ['CREATE TABLE']

        if exists_ok:
            builder.append('IF NOT EXISTS')

        builder.append(cls.__tablename__)
        builder.append('(%s)' % ', '.join(c._create_table() for c in cls.columns))
        statements.append(' '.join(builder) + ';')

        # handle the index creations
        for column in cls.columns:
            if column.index:
                fmt = 'CREATE INDEX IF NOT EXISTS {1.index_name} ON {0} ({1.name});'.format(cls.__tablename__, column)
                statements.append(fmt)

        return '\n'.join(statements)

    @classmethod
    async def insert(cls, connection=None, **kwargs):
        """Inserts an element to the table."""

        # verify column names:
        verified = {}
        for column in cls.columns:
            try:
                value = kwargs[column.name]
            except KeyError:
                continue

            check = column.column_type.python
            if value is None and not column.nullable:
                raise TypeError('Cannot pass None to non-nullable column %s.' % column.name)
            elif not check or not isinstance(value, check):
                fmt = 'column {0.name} expected {1.__name__}, received {2.__class__.__name__}'
                raise TypeError(fmt.format(column, check, value))

            verified[column.name] = value

        sql = 'INSERT INTO {0} ({1}) VALUES ({2});'.format(cls.__tablename__, ', '.join(verified),
                                                           ', '.join('$' + str(i) for i, _ in enumerate(verified, 1)))

        async with MaybeAcquire(connection, pool=cls._pool) as con:
            await con.execute(sql, *verified.values())

    @classmethod
    def to_dict(cls):
        x = {}
        x['name'] = cls.__tablename__
        x['__meta__'] = cls.__module__ + '.' + cls.__qualname__

        # nb: columns is ordered due to the ordered dict usage
        #     this is used to help detect renames
        x['columns'] = [a._to_dict() for a in cls.columns]
        return x

    @classmethod
    def from_dict(cls, data):
        meta = data['__meta__']
        given = cls.__module__ + '.' + cls.__qualname__
        if given != meta:
            cls = pydoc.locate(meta)
            if cls is None:
                raise RuntimeError('Could not locate "%s".' % meta)

        self = cls()
        self.__tablename__ = data['name']
        self.columns = [Column.from_dict(a) for a in data['columns']]
        return self

    @classmethod
    def all_tables(cls):
        return cls.__subclasses__()

    def diff(self, before):
        """Outputs the upgrade and downgrade path in JSON.

        This isn't necessarily good, but it outputs it in a format
        that allows the user to manually make edits if something is wrong.

        The following JSON schema is used:

        Note that every major key takes a list of objects as noted below.

        Note that add_column and drop_column automatically create and drop
        indices as necessary.

        changed_column_types:
            name: str [The column name]
            type: str [The new column type]
            using: Optional[str] [The USING expression to use, if applicable]
        add_columns:
            column: object
        remove_columns:
            column: object
        rename_columns:
            before: str [The previous column name]
            after:  str [The new column name]
        drop_index:
            name: str [The column name]
            index: str [The index name]
        add_index:
            name: str [The column name]
            index: str [The index name]
        changed_constraints:
            name: str [The column name]
            before:
                nullable: Optional[bool]
                default: Optional[str]
            after:
                nullable: Optional[bool]
                default: Optional[str]
        """
        upgrade = {}
        downgrade = {}

        def check_index_diff(a, b):
            if a.index != b.index:
                # Let's assume we have {name: thing, index: True}
                # and we're going to { name: foo, index: False }
                # This is a 'dropped' column when we upgrade with a rename
                # care must be taken to use the old name when dropping

                # check if we're dropping the index
                if not a.index:
                    # we could also be renaming so make sure to use the old index name
                    upgrade.setdefault('drop_index', []).append({ 'name': a.name, 'index': b.index_name })
                    # if we want to roll back, we need to re-add the old index to the old column name
                    downgrade.setdefault('add_index', []).append({ 'name': b.name, 'index': b.index_name })
                else:
                    # we're not dropping an index, instead we're adding one
                    upgrade.setdefault('add_index', []).append({ 'name': a.name, 'index': a.index_name })
                    downgrade.setdefault('drop_index', []).append({ 'name': a.name, 'index': a.index_name })

        def insert_column_diff(a, b):
            if a.column_type != b.column_type:
                if a.name == b.name and a.column_type.is_real_type() and b.column_type.is_real_type():
                    upgrade.setdefault('changed_column_types', []).append({ 'name': a.name, 'type': a.column_type.to_sql() })
                    downgrade.setdefault('changed_column_types', []).append({ 'name': a.name, 'type': b.column_type.to_sql() })
                else:
                    a_dict, b_dict = a._to_dict(), b._to_dict()
                    upgrade.setdefault('add_columns', []).append(a_dict)
                    upgrade.setdefault('remove_columns', []).append(b_dict)
                    downgrade.setdefault('remove_columns', []).append(a_dict)
                    downgrade.setdefault('add_columns', []).append(b_dict)
                    check_index_diff(a, b)
                    return

            elif a._is_rename(b):
                upgrade.setdefault('rename_columns', []).append({ 'before': b.name, 'after': a.name })
                downgrade.setdefault('rename_columns', []).append({ 'before': a.name, 'after': b.name })

            # technically, adding UNIQUE or PRIMARY KEY is rather simple and straight forward
            # however, since the inverse is a little bit more complicated (you have to remove
            # the index it maintains and you can't easily know what it is), it's not exactly
            # worth supporting any sort of change to the uniqueness/primary_key as it stands.
            # So.. just drop/add the column and call it a day.
            if a.unique != b.unique or a.primary_key != b.primary_key:
                a_dict, b_dict = a._to_dict(), b._to_dict()
                upgrade.setdefault('add_columns', []).append(a_dict)
                upgrade.setdefault('remove_columns', []).append(b_dict)
                downgrade.setdefault('remove_columns', []).append(a_dict)
                downgrade.setdefault('add_columns', []).append(b_dict)
                check_index_diff(a, b)
                return

            check_index_diff(a, b)

            b_qual, a_qual = b._qualifiers_dict(), a._qualifiers_dict()
            if a_qual != b_qual:
                upgrade.setdefault('changed_constraints', []).append({ 'name': a.name, 'before': b_qual, 'after': a_qual })
                downgrade.setdefault('changed_constraints', []).append({ 'name': a.name, 'before': a_qual, 'after': b_qual })

        if len(self.columns) == len(before.columns):
            # check if we have any changes at all
            for a, b in zip(self.columns, before.columns):
                if a._comparable_id == b._comparable_id:
                    # no change
                    continue
                insert_column_diff(a, b)

        elif len(self.columns) > len(before.columns):
            # check if we have more columns
            # typically when we add columns we add them at the end of
            # the table, this assumption makes this particularly bit easier.
            # Breaking this assumption will probably break this portion and thus
            # will require manual handling, sorry.

            for a, b in zip(self.columns, before.columns):
                if a._comparable_id == b._comparable_id:
                    # no change
                    continue
                insert_column_diff(a, b)

            new_columns = self.columns[len(before.columns):]
            added = [c._to_dict() for c in new_columns]
            upgrade.setdefault('add_columns', []).extend(added)
            downgrade.setdefault('remove_columns', []).extend(added)
        elif len(self.columns) < len(before.columns):
            # check if we have fewer columns
            # this one is a little bit more complicated

            # first we sort the columns by comparable IDs.
            sorted_before = sorted(before.columns, key=lambda c: c._comparable_id)
            sorted_after  = sorted(self.columns, key=lambda c: c._comparable_id)

            # handle the column diffs:
            for a, b in zip(sorted_after, sorted_before):
                if a._comparable_id == b._comparable_id:
                    continue
                insert_column_diff(a, b)

            # check which columns are 'left over' and remove them
            removed = [c._to_dict() for c in sorted_before[len(sorted_after):]]
            upgrade.setdefault('remove_columns', []).extend(removed)
            downgrade.setdefault('add_columns', []).extend(removed)

        return SchemaDiff(self, upgrade, downgrade)

async def _table_creator(tables, *, verbose=True):
    for table in tables:
        try:
            await table.create(verbose=verbose)
        except:
            log.error('Failed to create table %s.', table.__tablename__)

def create_tables(*tables, verbose=True, loop=None):
    if loop is None:
        loop = asyncio.get_event_loop()

    loop.create_task(_table_creator(tables, verbose=verbose))
