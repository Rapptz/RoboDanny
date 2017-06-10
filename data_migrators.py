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
