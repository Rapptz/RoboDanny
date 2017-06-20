# file for migrating the old files to the new SQL format
# precondition: tables created already
# function must be migrate_cog_name

import json
import datetime
import csv
import io

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
        await con.execute('TRUNCATE plonks, guild_mod_config RESTART IDENTITY;')

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

async def migrate_tags(pool, client):
    tags = _load_json('tags.json')

    # <location>:
    #   <name>: <data>

    # pretty straightforward port

    class TagData:
        __slots__ = ('name', 'content', 'owner_id', 'location_id', 'created_at', 'uses')

        def __init__(self, data):
            self.name = data['name']
            self.content = data['content']
            self.owner_id = int(data['owner_id'])
            location_id = data.get('location')
            self.uses = data.get('uses', 0)

            if location_id is not None and location_id != 'generic':
                self.location_id = int(location_id)
            else:
                self.location_id = None

            dt = data.get('created_at')
            if dt:
                self.created_at = datetime.datetime.fromtimestamp(dt).isoformat()
            else:
                self.created_at = datetime.datetime.utcnow().isoformat()

        def to_record(self):
            return tuple(getattr(self, attr) for attr in self.__slots__)

    class TagLookupData:
        __slots__ = ('name', 'tag_id', 'owner_id', 'created_at', 'location_id')

        def __init__(self, data, location, tag_data):
            self.name = data['name']
            self.owner_id = int(data['owner_id'])
            self.location_id = location

            dt = data.get('created_at')
            if dt:
                self.created_at = datetime.datetime.fromtimestamp(dt).isoformat()
            else:
                self.created_at = datetime.datetime.utcnow().isoformat()

            original = data['original']

            for index, tag in enumerate(tag_data, 1):
                if tag.location_id == location and tag.name == original:
                    self.tag_id = index
                    break
            else:
                raise RuntimeError('Bad tag alias.')

        @classmethod
        def from_tag_pair(cls, index, tag):
            self = cls.__new__(cls)
            self.name = tag.name
            self.tag_id = index
            self.created_at = tag.created_at
            self.owner_id = tag.owner_id
            self.location_id = tag.location_id
            return self

        def to_record(self):
            return tuple(getattr(self, attr) for attr in self.__slots__)

        def _key(self):
            return (self.name.lower(), self.location_id)

    tag_data = [
        TagData(data)
        for location, obj in tags.items()
            for name, data in obj.items()
                if '__tag__' in data
                # if location.isdigit() and client.get_guild(int(location)) is not None
    ]

    lookup = []

    for location, obj in tags.items():
        location_id = None if not location.isdigit() else int(location)

        # if client.get_guild(location_id) is None:
            # continue

        for name, data in obj.items():
            if '__tag_alias__' not in data:
                continue

            try:
                lookup_data = TagLookupData(data, location_id, tag_data)
            except RuntimeError:
                continue
            else:
                lookup.append(lookup_data)

    for index, tag in enumerate(tag_data, 1):
        if tag.location_id is not None:
            lookup.append(TagLookupData.from_tag_pair(index, tag))

    # due to a bug in ?tag make some duplicates are added for whatever reason
    # we'll just take one off at random and hope for the best
    # essentially, if someone edited the original 'name' message before
    # committing it with a 'content' message, the cache would update and
    # we'd use the new message content

    seen = set()
    seen_add = seen.add
    lookup = [x for x in lookup if not (x._key() in seen or seen_add(x._key()))]

    async with pool.acquire() as con:
        # delete the current tags
        await con.execute('TRUNCATE tags, tag_lookup RESTART IDENTITY;')
        async with con.transaction():
            stream = io.StringIO()
            writer = csv.writer(stream, quoting=csv.QUOTE_MINIMAL)

            for r in tag_data:
                writer.writerow(r.to_record())

            obj = io.BytesIO(stream.getvalue().encode())

            print(await con.copy_to_table('tags', columns=TagData.__slots__, source=obj, format='csv'))

            stream = io.StringIO()
            writer = csv.writer(stream, quoting=csv.QUOTE_MINIMAL)

            for r in lookup:
                writer.writerow(r.to_record())

            obj = io.BytesIO(stream.getvalue().encode())
            print(await con.copy_to_table('tag_lookup', columns=TagLookupData.__slots__, source=obj, format='csv'))
