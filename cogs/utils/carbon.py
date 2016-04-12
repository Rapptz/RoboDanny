import threading
import requests
import time
import logging

log = logging.getLogger()

# This uses a background thread instead of a task

class CarbonStatistics(threading.Thread):
    def __init__(self, key, bot, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.key = key
        self.bot = bot
        self.daemon = True

    def run(self):
        url = 'https://www.carbonitex.net/discord/data/botdata.php'
        while True:
            data = {
                'key': self.key,
                'servercount': len(self.bot.servers)
            }

            try:
                resp = requests.post(url, json=data)
                log.info('Carbon statistics returned {0.status_code} for {1}'.format(resp, data))
            except Exception as e:
                log.error('An error occurred while fetching statistics: ' + str(e))
            finally:
                time.sleep(30 * 60) # send statistics every 30 minutes
