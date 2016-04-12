## R. Danny

A personal bot that runs on Discord.

## Running

I would prefer if you don't run an instance of my bot. Just call the join command with an invite URL to have it on your server. The source here is provided for educational purposes for discord.py.

However...

You should only need two main configuration files while the rest will be created automatically.

First is a credentials.json file with the credentials:

```js
{
    "token": "your bot token",
    "client_id": "your client_id",
    "carbon_key": "the key for carbonitex"
}
```

Second is a splatoon.json file with your Nintendo Network credentials and Splatoon-related data.

```js
{
    "username": "nnid",
    "password": "nnid password",
    "maps": [
        "Urchin Underpass",
        ...
    ],
    "abilities": [
        "Bomb Range Up",
        ...
    ],
    "weapons": [
        {
            "special": "Kraken",
            "sub": "Burst Bomb",
            "name": "L-3 Nozzlenose D"
        },
        ...
    ],
    "brands": [
        {
            "nerfed": null,
            "buffed": null,
            "name": "Famitsu"
        },
        {
            "nerfed": "Ink Recovery Up",
            "buffed": "Ink Saver (Sub)",
            "name": "Firefin"
        },
        ...
    ]
}
```

After you do the setup required just edit the `cogs/utils/checks.py` file with your owner ID.

## Requirements

- Python 3.5+
- Async version of discord.py
- lxml
