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
        await con.execute('TRUNCATE rtfm, feeds RESTART IDENTITY;')
        records = [(int(k), v) for k, v in rtfm.items() if client.get_user(int(k))]
        status = await con.copy_records_to_table('rtfm', records=records, columns=('user_id', 'count'))
        print('[rtfm]', status)

        records = [(int(channel_id), int(role_id), name)
                   for channel_id, data in feeds.items()
                        for name, role_id in data.items()]

        status = await con.copy_records_to_table('feeds', records=records, columns=('channel_id', 'role_id', 'name'))
        print('[feeds]', status)

async def migrate_mod(pool, client):
    # NOTE: plonks table is technically in config, but most of its data
    # comes from this table
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

        status = await con.copy_records_to_table('plonks', records=records, columns=('guild_id', 'entity_id'))
        print('[plonks]', status)
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

        columns = ('id', *Holder.__slots__)
        print('[guild_mod_config]', await con.copy_records_to_table('guild_mod_config', records=records, columns=columns))

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
                self.created_at = datetime.datetime.fromtimestamp(dt)
            else:
                self.created_at = datetime.datetime.utcnow()

        def to_record(self):
            return tuple(getattr(self, attr) for attr in self.__slots__)

        def _key(self):
            return (self.name.lower(), self.location_id)

    class TagLookupData:
        __slots__ = ('name', 'tag_id', 'owner_id', 'created_at', 'location_id')

        def __init__(self, data, location, tag_data):
            self.name = data['name']
            self.owner_id = int(data['owner_id'])
            self.location_id = location

            dt = data.get('created_at')
            if dt:
                self.created_at = datetime.datetime.fromtimestamp(dt)
            else:
                self.created_at = datetime.datetime.utcnow()

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

    seen = set()
    seen_add = seen.add
    tag_data = [x for x in tag_data if not (x._key() in seen or seen_add(x._key()))]

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
            records = [r.to_record() for r in tag_data]
            print('[tags]', await con.copy_records_to_table('tags', records=records, columns=TagData.__slots__))

            records = [r.to_record() for r in lookup]
            status = await con.copy_records_to_table('tag_lookup', records=records, columns=TagLookupData.__slots__)
            print('[tag_lookup]', status)

async def migrate_config(pool, client):
    perms = _load_json('permissions.json')

    async with pool.acquire() as con:
        await con.execute('TRUNCATE command_config;')

        records = [
            (int(k), name, False)
            for k, db in perms.items()
                for name in db
                    if client.get_guild(int(k))
        ]

        await con.copy_records_to_table('command_config', records=records, columns=('guild_id', 'name', 'whitelist'))

async def migrate_stars(pool, client):
    # config format: (yeah, it's not ideal or really any good but whatever)
    # <guild_id> : <data> where <data> is
    # channel: <starboard channel id>
    # locked: <boolean indicating locked status>
    # message_id: [bot_message, [starred_user_ids]]
    stars = {int(k): v for k, v in _load_json('stars.json').items() }

    class Entry:
        __slots__ = ('bot_message_id', 'message_id', 'guild_id')
        def __init__(self, guild_id, message_id, bot_message_id):
            self.bot_message_id = int(bot_message_id)
            self.guild_id = guild_id
            self.message_id = int(message_id)

        def to_record(self):
            return tuple(getattr(self, attr) for attr in self.__slots__)

        # def __hash__(self):
        #     return hash(self.message_id)

        # def __eq__(self, o):
        #     return isinstance(o, Entry) and o.message_id == self.message_id

    class Starrer:
        __slots__ = ('author_id', 'entry_id')
        def __init__(self, author_id, entry_id):
            self.author_id = int(author_id)
            self.entry_id = entry_id

        def to_record(self):
            return tuple(getattr(self, attr) for attr in self.__slots__)

    new_data = {}

    async with pool.acquire() as con:
        # the incredibly basic case, creating the starboard table
        await con.execute("TRUNCATE starboard, starboard_entries, starrers RESTART IDENTITY;")

        records = []
        for guild_id in stars:
            # we do not care about 'locked' status when porting
            stars[guild_id].pop('locked', None)
            guild = client.get_guild(guild_id)
            if not guild:
                continue

            channel_id = stars[guild_id].pop('channel', None)
            if channel_id is None:
                continue

            channel_id = int(channel_id)
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue

            records.append((guild_id, channel_id, None))
            new_data[guild_id] = stars[guild_id]

        status = await con.copy_records_to_table('starboard', columns=('id', 'channel_id', 'locked'), records=records)
        print('Starboard', status)

        entries = []
        starrers = []
        for guild_id, value in new_data.items():
            for message_id, data in value.items():
                if not isinstance(data, list):
                    continue

                bot_message_id, rest = data
                if bot_message_id is None:
                    continue

                if not isinstance(rest, list):
                    continue

                entries.append(Entry(guild_id, message_id, bot_message_id))
                entry_id = len(entries)
                for author_id in rest:
                    starrers.append(Starrer(author_id, entry_id))

        # actually port now
        records = [entry.to_record() for entry in entries]
        status = await con.copy_records_to_table('starboard_entries', columns=Entry.__slots__, records=records)
        print('Starboard Entries', status)

        records = [r.to_record() for r in starrers]
        status = await con.copy_records_to_table('starrers', columns=Starrer.__slots__, records=records)
        print('Starboard Starrers', status)

async def migrate_profile(pool, client):
    # note: also porting over pokemon.json
    friend_codes = _load_json('pokemon.json').get('friend_codes', {})
    profiles = _load_json('profiles.json')

    class ProfileEntry:
        __slots__ = ('id', 'nnid', 'squad', 'extra', 'fc_3ds')

        def __init__(self, user_id, profile, fc):
            self.id = int(user_id)
            self.squad = profile.get('squad')
            self.nnid = profile.get('nnid')
            self.fc_3ds = fc

            extra = {}
            rank = profile.get('rank')
            if rank:
                extra['sp1_rank'] = rank

            weapon = profile.get('weapon')
            if weapon:
                extra['sp1_weapon'] = weapon

            self.extra = json.dumps(extra)

        def to_record(self):
            return tuple(getattr(self, a) for a in self.__slots__)

    data = []

    for key, value in profiles.items():
        code = friend_codes.pop(key, None)
        data.append(ProfileEntry(key, value, code))

    for key, value in friend_codes.items():
        data.append(ProfileEntry(key, {}, value))

    async with pool.acquire() as con:
        await con.execute("TRUNCATE profiles RESTART IDENTITY;")
        records = [r.to_record() for r in data]
        status = await con.copy_records_to_table('profiles', columns=ProfileEntry.__slots__, records=records)
        print('Profiles', status)


async def migrate_emoji(pool, client):
    # guild_id: <data>
    # emoji_id: count
    stats = _load_json('emoji_statistics.json')

    # this is hella easy
    async with pool.acquire() as con:
        await con.execute("TRUNCATE emoji_stats RESTART IDENTITY;")

        records = [
            (int(guild_id), int(emoji_id), count)
            for guild_id, data in stats.items()
            for emoji_id, count in data.items()
        ]

        status = await con.copy_records_to_table('emoji_stats', columns=('guild_id', 'emoji_id', 'total'), records=records)
        print('Emoji Statistics', status)
