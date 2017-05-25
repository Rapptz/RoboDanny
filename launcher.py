import click
import logging
import asyncio
import contextlib
from bot import RoboDanny

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
    fmt = logging.Formatter('[{levelname:<8}]: [{asctime}] {name}: {message}', dt_fmt, style='{')
    handler.setFormatter(fmt)
    log.addHandler(handler)

    yield

    # __exit__
    logging.shutdown()

@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    if ctx.invoked_subcommand is None:
        with setup_logging():
            bot = RoboDanny()
            bot.run()

if __name__ == '__main__':
    main()
