import sys
import click
import logging
import asyncio
import asyncpg
import importlib
import contextlib

from bot import RoboDanny, initial_extensions
from cogs.utils import db

from pathlib import Path

import config
import traceback

try:
    import uvloop
except ImportError:
    pass
else:
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

@contextlib.contextmanager
def setup_logging():
    # __enter__
    logging.getLogger('discord').setLevel(logging.INFO)
    logging.getLogger('discord.http').setLevel(logging.DEBUG)

    log = logging.getLogger()
    log.setLevel(logging.INFO)
    handler = logging.FileHandler(filename='rdanny.log', encoding='utf-8', mode='w')
    dt_fmt = '%Y-%m-%d %H:%M:%S'
    fmt = logging.Formatter('[{asctime}] [{levelname:<7}] {name}: {message}', dt_fmt, style='{')
    handler.setFormatter(fmt)
    log.addHandler(handler)

    yield

    # __exit__
    logging.shutdown()

def run():
    loop = asyncio.get_event_loop()
    log = logging.getLogger()

    try:
        pool = loop.run_until_complete(db.Table.create_pool(config.postgres, command_timeout=60))
    except Exception as e:
        click.echo('Could not set up PostgreSQL. Exiting.', file=sys.stderr)
        log.exception('Could not set up PostgreSQL. Exiting.')
        return

    bot = RoboDanny()
    bot.pool = pool
    bot.run()

@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """Launches the bot."""
    if ctx.invoked_subcommand is None:
        loop = asyncio.get_event_loop()
        with setup_logging():
            run()

@main.command(short_help='initialises the databases for the bot')
@click.option('-e', '--extension', help='which extension to initialise DB for', multiple=True)
@click.option('-q', '--quiet', help='less verbose output', is_flag=True)
def initdb(extension, quiet):
    """This manages the migrations and database creation system for you.

    Typically this is done automatically on cog load, but sometimes
    when developing we don't get that luxury.
    """
    run = asyncio.get_event_loop().run_until_complete
    try:
        run(db.Table.create_pool(config.postgres))
    except Exception:
        click.echo('Could not create PostgreSQL connection pool.\n' + traceback.format_exc(), err=True)
        return

    if not extension:
        extension = initial_extensions

    for ext in extension:
        try:
            importlib.import_module(ext)
        except Exception:
            click.echo('Could not load %s.\n%s' % (ext, traceback.format_exc()), err=True)

    for table in db.Table.all_tables():
        try:
            run(table.create(verbose=not quiet))
        except Exception:
            click.echo('Could not create %s.\n%s' % (table.__tablename__, traceback.format_exc()), err=True)
        else:
            click.echo('[{0.__module__}] Processing creation or migration for {0.__tablename__} complete.'.format(table))

async def remove_database(name):
    try:
        con = await asyncpg.connect(config.postgres)
    except:
        pass
    else:
        # I know that looks odd, but I can't use $1 with DROP TABLE.
        await con.execute('DROP TABLE %s;' % name)
        await con.close()

@main.command(short_help='removes a table')
@click.argument('name')
@click.option('-q', '--quiet', help='less verbose output', is_flag=True)
def dropdb(name, quiet):
    """This removes a database and all its migrations.

    You must be pretty sure about this before you do it,
    as once you do it there's no coming back.
    """

    run = asyncio.get_event_loop().run_until_complete

    migration = Path('migrations').joinpath(name).with_suffix('.json')
    current = migration.with_name('current-' + migration.name)

    click.confirm('do you really want to do this?', abort=True)

    try:
        run(remove_database(name))
    except Exception:
        click.echo('could not delete the database\n' + traceback.format_exc(), err=True)
        return

    if not migration.exists() or not current.exists():
        click.echo('warning: could not find the appropriate files.')

    try:
        migration.unlink()
    except:
        click.echo('warning: could not delete migration file')

    try:
        current.unlink()
    except:
        click.echo('warning: could not delete current migration file')

    click.echo('successfully removed %s database' % name)

@main.command(short_help='migrates from JSON files')
@click.argument('name')
@click.pass_context
def convertjson(ctx, name):
    """This migrates our older JSON files to PostgreSQL

    You can pass "all" as the name to migrate everything
    instead of a single migration.

    Note, this deletes all previous entries in the table
    so you can consider this to be a destructive decision.

    The point of this is just to do some migration of the
    data from v3 -> v4 once and call it a day.
    """

    import data_migrators

    run = asyncio.get_event_loop().run_until_complete

    if name == 'all':
        to_run = [(getattr(data_migrators, attr), attr.replace('migrate_', ''))
                  for attr in dir(data_migrators) if attr.startswith('migrate_')]
    else:
        try:
            to_run = getattr(data_migrators, 'migrate_' + name)
        except AttributeError:
            click.echo('invalid cog name given, %s.' % name, err=True)
            return
        to_run = [(to_run, name)]

    async def create_pool():
        return await asyncpg.create_pool(config.postgres)

    try:
        pool = run(create_pool())
    except Exception:
        click.echo('Could not create PostgreSQL connection pool.\n' + traceback.format_exc(), err=True)
        return

    extensions = ['cog.' + name for _, name in to_run]
    ctx.invoke(initdb, extension=extensions)

    for migrator, _ in to_run:
        try:
            run(migrator(pool))
        except Exception:
            click.echo('migrator %s has failed, terminating\n%s' % (migrator.__name__, traceback.format_exc()), err=True)
            return
        else:
            click.echo('migrator %s completed successfully' % migrator.__name__)

if __name__ == '__main__':
    main()
