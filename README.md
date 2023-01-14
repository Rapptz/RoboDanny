## R. Danny

A personal bot that runs on Discord.

## Running

I would prefer if you don't run an instance of my bot. Just call the join command with an invite URL to have it on your server. The source here is provided for educational purposes for discord.py.

Nevertheless, the installation steps are as follows:

1. **Make sure to get Python 3.8 or higher**

This is required to actually run the bot.

2. **Set up venv**

Just do `python3.8 -m venv venv`

3. **Install dependencies**

This is `pip install -U -r requirements.txt`

4. **Create the database in PostgreSQL**

You will need PostgreSQL 9.5 or higher and type the following
in the `psql` tool:

```sql
CREATE ROLE rdanny WITH LOGIN PASSWORD 'yourpw';
CREATE DATABASE rdanny OWNER rdanny;
CREATE EXTENSION pg_trgm;
```

5. **Setup configuration**

The next step is just to create a `config.py` file in the root directory where
the bot is with the following template:

```py
client_id   = '' # your bot's client ID
token = '' # your bot's token
debug = False # used to disable certain features
carbon_key = '' # your bot's key on carbon's site
bots_key = '' # your key on bots.discord.pw
postgresql = 'postgresql://user:password@host/database' # your postgresql info from above
challonge_api_key = '...' # for tournament cog
stat_webhook = ('<webhook_id>','<webhook_token>') # a webhook to a channel for bot stats.
# when you generate your webhook, take the token and ID from the URL like so:
# https://discord.com/api/webhooks/<id>/<token>
github_token = '' # your github API personal token
open_collective_token = '' # your open collective personal token
oc_discord_client_id = '' # the client ID of the Open Collective Discord Integration application
oc_discord_client_secret = '' # the client secret of the Open Collective Discord Integration application
```

A lot of these configuration variables are undocumented precisely because the bot is meant for personal use.

6. **Configuration of database**

To configure the PostgreSQL database for use by the bot, go to the directory where `launcher.py` is located, and run the script by doing `python3.8 launcher.py db init`

## Privacy Policy and Terms of Service

Discord requires me to make one of these.

There isn't really anything to note. No personal data is stored.
