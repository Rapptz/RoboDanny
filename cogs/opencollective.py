from __future__ import annotations
import traceback
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict
from discord.ext import commands, tasks
import logging
import discord
import datetime
import math

if TYPE_CHECKING:
    from bot import RoboDanny
    from typing_extensions import NotRequired


WEBHOOK_CHANNEL_ID = 1047006792338653184

_log = logging.getLogger(__name__)


class TokenRevoked(Exception):
    """Exception used when the refresh token has been revoked by the user"""

    pass


class DiscordTokenResponse(TypedDict):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    scope: str


class OpenCollectiveMetadata(TypedDict):
    # Discord requires 0=false, 1=true
    is_backer: int

    total_donated: NotRequired[int]
    last_donation: NotRequired[str]
    last_donation_amount: NotRequired[int]


class OpenCollectiveContributor:
    __slots__ = ('id', 'name', 'slug', 'total_donated', 'donation_amount', 'last_donation', 'donator_since')

    def __init__(self, payload: dict[str, Any]) -> None:
        from_account = payload['fromAccount']
        self.id: str = from_account['id']
        self.name: str = from_account['name']
        self.slug: str = from_account['slug']
        member_of = from_account['memberOf']['nodes'][0]
        self.total_donated: int = math.ceil(member_of['totalDonations']['value'])
        self.donation_amount: int = math.ceil(payload['amountInHostCurrency']['value'])
        self.last_donation: datetime.datetime = datetime.datetime.fromisoformat(payload['createdAt'].replace('Z', '+00:00'))
        self.donator_since: datetime.datetime = datetime.datetime.fromisoformat(member_of['since'].replace('Z', '+00:00'))

    def to_metadata(self) -> OpenCollectiveMetadata:
        return {
            'is_backer': 1,
            'total_donated': self.total_donated,
            'last_donation': self.last_donation.isoformat(),
            'last_donation_amount': self.donation_amount,
        }


class OpenCollectiveSyncRecord(TypedDict):
    id: int
    name: str
    slug: str
    account_id: str
    refresh_token: str
    access_token: str
    expires_at: datetime.datetime


class Credentials(NamedTuple):
    user_id: int
    id: str
    name: str
    slug: str
    access_token: str
    refresh_token: str
    expires_in: int

    @classmethod
    def from_embed(cls, embed: discord.Embed):
        # Some validation...
        if embed.title is None:
            return None

        if len(embed.fields) < 6:
            return None

        if (
            embed.fields[0].value is not None
            and embed.fields[1].value is not None
            and embed.fields[2].value is not None
            and embed.fields[3].value is not None
            and embed.fields[4].value is not None
            and embed.fields[5].value is not None
        ):
            return cls(
                user_id=int(embed.title),
                id=embed.fields[0].value,
                name=embed.fields[1].value,
                slug=embed.fields[2].value,
                access_token=embed.fields[3].value,
                refresh_token=embed.fields[4].value,
                expires_in=int(embed.fields[5].value),
            )

        return None


class OpenCollective(commands.Cog):
    def __init__(self, bot: RoboDanny) -> None:
        self.bot: RoboDanny = bot

    async def cog_load(self) -> None:
        self.update_contributor_metadata.start()

    async def cog_unload(self) -> None:
        self.update_contributor_metadata.stop()

    async def load_collective_data(self) -> dict[str, OpenCollectiveContributor]:
        delta = discord.utils.utcnow() - datetime.timedelta(days=2)
        query = """
        query collective($slug: String, $date: DateTime) {
            collective(slug: $slug) {
                transactions(limit: 100, type: CREDIT, dateFrom: $date) {
                    totalCount
                    nodes {
                        fromAccount {
                            id
                            name
                            slug
                            memberOf (account: {slug: $slug}) {
                                nodes {
                                    totalDonations {
                                        value
                                    }
                                    since
                                }
                            }
                        }
                        amountInHostCurrency {
                            value
                        }
                        createdAt
                    }
                }
            }
        }
        """

        params = {
            'personalToken': self.bot.config.open_collective_token,
        }

        payload = {
            'query': query,
            'variables': {
                'slug': 'discordpy',
                'date': delta.isoformat(),
            },
        }

        contributors: dict[str, OpenCollectiveContributor] = {}
        async with self.bot.session.post('https://api.opencollective.com/graphql/v2', params=params, json=payload) as resp:
            if resp.status != 200:
                await self.log_error(f'Failed to fetch Open Collective data: {resp.status}')
                return {}

            data = await resp.json()
            transactions = data['data']['collective']['transactions']['nodes']

            # These are (thankfully) already sorted by latest transaction
            for transaction in transactions:
                member = OpenCollectiveContributor(transaction)

                # If we already registered this one then skip it
                if member.id in contributors:
                    continue

                contributors[member.id] = member

        return contributors

    async def refresh_access_token(self, record: OpenCollectiveSyncRecord) -> DiscordTokenResponse:
        url = 'https://discord.com/api/v10/oauth2/token'
        data = {
            'client_id': self.bot.config.oc_discord_client_id,
            'client_secret': self.bot.config.oc_discord_client_secret,
            'grant_type': 'refresh_token',
            'refresh_token': record['refresh_token'],
        }

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
        }

        async with self.bot.session.post(url, data=data, headers=headers) as resp:
            data = await resp.json()
            if resp.status != 200 and 'error' in data and data['error'] == 'invalid_grant':
                raise TokenRevoked

            if resp.status != 200:
                raise RuntimeError(f'Discord responded with non-200 {resp.status}')

            return data

    async def update_contributor_access_tokens(self, tokens: DiscordTokenResponse, user_id: int) -> None:
        query = """UPDATE open_collective_sync
                   SET access_token = $1, refresh_token = $2, expires_at = $3
                   WHERE id = $4
                """

        expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=tokens['expires_in'])
        await self.bot.pool.execute(query, tokens['access_token'], tokens['refresh_token'], expires_at, user_id)

    async def delete_contributor_link(self, user_id: int) -> None:
        query = "DELETE FROM open_collective_sync WHERE id = $1"
        await self.bot.pool.execute(query, user_id)

    async def sync_contributor(self, record: OpenCollectiveSyncRecord, contributor: OpenCollectiveContributor) -> None:
        now = datetime.datetime.utcnow()
        access_token = record['access_token']
        if now > record['expires_at']:
            try:
                tokens = await self.refresh_access_token(record)
            except TokenRevoked:
                await self.delete_contributor_link(record['id'])
            except Exception as e:
                # Unknown error, just skip for now.
                await self.log_error('Unknown error while refreshing access token', error=e, record=repr(record))
                return
            else:
                access_token = tokens['access_token']

        metadata = contributor.to_metadata()
        url = f'https://discord.com/api/v10/users/@me/applications/{self.bot.config.oc_discord_client_id}/role-connection'
        payload = {
            'platform_name': 'Open Collective',
            'platform_username': contributor.name,
            'metadata': metadata,
        }

        async with self.bot.session.put(url, json=payload, headers={'Authorization': f'Bearer {access_token}'}) as resp:
            if resp.status != 200:
                await self.log_error(
                    f'Failed to update contributor metadata to Discord: {resp.status}',
                    record=repr(record),
                    metadata=repr(metadata),
                )
                return

    @tasks.loop(hours=12)
    async def update_contributor_metadata(self) -> None:
        contributors = await self.load_collective_data()

        # Don't do anything if there isn't any new data
        if len(contributors) == 0:
            _log.info('No contributions found to sync')
            return

        credentials_query = "SELECT * FROM open_collective_sync WHERE account_id = ANY($1::text[])"
        records: list[OpenCollectiveSyncRecord] = await self.bot.pool.fetch(credentials_query, list(contributors.keys()))
        for record in records:
            contributor = contributors[record['account_id']]
            await self.sync_contributor(record, contributor)

        _log.info('Finished syncing %s Open Collective contributors', len(records))

    async def log_error(self, message: str, *, error: Exception | None = None, **fields: str) -> None:
        e = discord.Embed(title='Open Collective Sync Error', colour=0xDD5F53)
        if error is not None:
            exc = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
            e.description = f'```py\n{exc}\n```'

        e.add_field(name='Message', value=message, inline=False)
        for name, value in fields.items():
            e.add_field(name=name, value=value, inline=True)

        try:
            await self.bot.stats_webhook.send(embed=e)
        except Exception:
            # If anything happened here, just discard is completely.
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.channel.id != WEBHOOK_CHANNEL_ID:
            return

        if not message.author.bot:
            return

        if not message.embeds:
            return

        embed = message.embeds[0]
        credentials = Credentials.from_embed(embed)
        if credentials is None:
            return

        query = """
            INSERT INTO open_collective_sync (id, name, slug, account_id, access_token, refresh_token, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE
            SET
                name = EXCLUDED.name,
                slug = EXCLUDED.slug,
                account_id = EXCLUDED.account_id,
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                expires_at = EXCLUDED.expires_at
        """
        expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=credentials.expires_in)
        await self.bot.pool.execute(
            query,
            credentials.user_id,
            credentials.name,
            credentials.slug,
            credentials.id,
            credentials.access_token,
            credentials.refresh_token,
            expires_at,
        )


async def setup(bot: RoboDanny) -> None:
    await bot.add_cog(OpenCollective(bot))
