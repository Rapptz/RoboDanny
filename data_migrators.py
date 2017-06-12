# file for migrating the old files to the new SQL format
# precondition: tables created already
# function must be migrate_cog_name

import json

def _load_json(fp):
    with open(fp, 'r', encoding='utf-8') as f:
        return json.load(f)

async def migrate_api(pool, client):
    rtfm = _load_json('rtfm.json').get('users', {})
    feeds = _load_json('feeds.json')

    async with pool.acquire() as con:
        await con.execute('DELETE FROM rtfm;\nDELETE FROM feeds;')
        records = [(int(k), v) for k, v in rtfm.items() if client.get_user(int(k))]
        await con.copy_records_to_table('rtfm', records=records, columns=('user_id', 'count'))

        records = [(int(channel_id), int(role_id), name)
                   for channel_id, data in feeds.items()
                        for name, role_id in data.items()]

        await con.copy_records_to_table('feeds', records=records, columns=('channel_id', 'role_id', 'name'))

async def migrate_mod(pool, client):
    config = _load_json('mod.json')

    # guild_id: [raid_mode, broadcast_channel]
    raids = config.get('__raids__', {})

    # guild_id: [user_ids]
    plonks = config.get('plonks', {})

    # [channel_id]
    # so let's convert to guild_id: [channel_id]
    ignored = {}
    for item in config.get('ignored', []):
        ch = client.get_channel(int(item))
        if ch:
            ignored.setdefault(ch.guild.id, []).append(ch.id)

    # guild_id: data
    # data ->
    # count: <num>
    # ignore: [channel_id]
    mentions = config.get('mentions', {})

    async with pool.acquire() as con:
        await con.execute('DELETE FROM plonks;\nDELETE FROM guild_mod_config;')

        # port plonks and ignores

        # start with plonks
        records = [
            (int(k), int(v))
            for k, data in plonks.items()
                for v in data
                    if client.get_guild(int(k))
        ]

        # port ignores
        records.extend(
            (guild_id, entity_id)
            for guild_id, channels in ignored.items()
                for entity_id in channels
        )

        await con.copy_records_to_table('plonks', records=records, columns=('guild_id', 'entity_id'))

        # a bit more complex, the guild_mod_config table

        class Holder:
            __slots__ = ('raid_mode', 'broadcast_channel', 'mention_count', 'safe_mention_channel_ids')

            def __init__(self):
                for v in self.__slots__:
                    setattr(self, v, None)

        # time to join all guild_id available:
        guild_ids = {*raids, *mentions}
        data = { k: Holder() for k in guild_ids }

        # port raid mode stuff
        for guild_id, (raid_mode, broadcast_channel) in raids.items():
            value = data[guild_id]
            value.raid_mode = raid_mode
            value.broadcast_channel = broadcast_channel and int(broadcast_channel)

        # port mention stuff
        for guild_id, elem in mentions.items():
            count = elem['count']
            ignore = elem.get('ignore', [])

            value = data[guild_id]
            value.mention_count = count
            value.safe_mention_channel_ids = [int(x) for x in ignore if client.get_channel(int(x))]

        # actually port it over now
        records = [
            (int(k), v.raid_mode, v.broadcast_channel, v.mention_count, v.safe_mention_channel_ids)
            for k, v in data.items()
                if client.get_guild(int(k))
        ]

        # it turns out that due to an issue, we can't use copy_records_to_table
        # see: https://github.com/MagicStack/asyncpg/issues/153

        # columns = ('id', *Holder.__slots__)
        # await con.copy_records_to_table('guild_mod_config', records=records, columns=columns)

        # so let's just mass-insert :^(
        async with con.transaction():
            query = """INSERT INTO guild_mod_config
                              (id, raid_mode, broadcast_channel, mention_count, safe_mention_channel_ids)
                       VALUES ($1, $2, $3, $4, $5);
                    """

            await con.executemany(query, records)
