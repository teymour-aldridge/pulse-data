# Recidiviz - a platform for tracking granular recidivism metrics in real time
# Copyright (C) 2018 Recidiviz, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
# =============================================================================

"""Runs a scraper in memory.

usage: run_scraper.py [-h] --region REGION [--num_tasks NUM_TASKS]
                      [--sleep_between_requests SLEEP_BETWEEN_REQUESTS]

Example:
python -m recidiviz.ingest.run_scraper --region us_pa_greene
python -m recidiviz.ingest.run_scraper --region us_pa_greene --num_tasks 10
"""

import argparse
import datetime
import logging
import time
import types
from google.appengine.api import apiproxy_stub_map
from google.appengine.api import urlfetch_stub
from recidiviz.ingest import constants
from recidiviz.utils import regions

# Sleep 1 seconds per task
sleep_between_requests = 1

# Number of people to scrape by default
num_tasks_left = 5

# This function acts as a bound method to the scraper instance.  It is
# overwriting the functionality of add_task to just run the task instead of
# throwing it on the taskqueue.  By binding it in this fashion, we get access
# to self as if this function was defined inline in the class, which is useful
# in faking the run.
def add_task(self, task_name, params):
    """Overwritten version of add task which simply runs the task
    and ignores the usage of the task_queue."""
    logging.info('***')
    # These must be global because we are binding the function and we therefore
    # have no control over how they're called.  The choices would be to make
    # the bound function inline, or use the global counters in this fashion.
    # Doing it inline doesn't make much sense in this use case because thhis is
    # a CLI with all static functions, and the entrypoint is module level.
    global num_tasks_left
    global sleep_between_requests
    num_tasks_left -= 1
    # If we are done, we can exit.
    if num_tasks_left == 0:
        logging.info('Completed the test run!')
        exit()
    else:
        logging.info('%s tasks left to complete' % num_tasks_left)
        logging.info(
            'Sleeping %s seconds before sending another request' %
            sleep_between_requests)
        time.sleep(sleep_between_requests)
    task_type = params.get('task_type', self.get_initial_task_type())
    endpoint = params.get('endpoint', self.get_initial_endpoint() if
                          self.is_initial_task(task_type) else None)
    logging.info('Adding a task with endpoint: %s' % endpoint)
    logging.info('***')
    fn = getattr(self, task_name)
    fn(params)


def start_scrape(self, scrape_type):
    fn = getattr(self, self.get_initial_task())
    fn({'scrape_type': scrape_type,
        'scraper_start_time': datetime.datetime.now()})


def _create_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--region', required=True, help='The region to test')
    parser.add_argument(
        '--num_tasks', required=False,
        default=num_tasks_left,
        help='The number of tasks to complete, default is {}'.format(
            num_tasks_left))
    parser.add_argument(
        '--sleep_between_requests', required=False,
        default=sleep_between_requests,
        help='The number of seconds to sleep in between requests,'
             'default is {}'.format(sleep_between_requests))
    return parser


def _configure_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("urlfetch").setLevel(logging.WARNING)


if __name__ == "__main__":
    arg_parser = _create_parser()
    args = arg_parser.parse_args()

    num_tasks_left = args.num_tasks
    sleep_between_requests = args.sleep_between_requests

    _configure_logging()

    # TODO: remove this once appengine is removed
    # I have no clue why this is needed, but it is needed for in memory unit
    # tests when importing appengine.
    apiproxy_stub_map.apiproxy = apiproxy_stub_map.APIProxyStubMap()
    apiproxy_stub_map.apiproxy.RegisterStub(
        'urlfetch', urlfetch_stub.URLFetchServiceStub())

    region = regions.Region(args.region)
    scraper = region.get_scraper()

    # We use this to bind the method to the instance.
    scraper.add_task = types.MethodType(add_task, scraper)
    scraper.start_scrape = types.MethodType(start_scrape, scraper)

    scraper.start_scrape(constants.BACKGROUND_SCRAPE)