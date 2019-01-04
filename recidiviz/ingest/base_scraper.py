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


"""Generic implementation of the scraper.  This class abstracts away the need
for scraper implementations to know or understand tasks.  Child scrapers need
only to know how to extract data from a given page and whether or not more pages
need to be scraped.

This is a class that aims to handle as many DB related operations as possible
as well as operations around how to get the content of a page.  It provides
generic implementations of getter functions which aim to scrape out important
fields we care about.

In order to sublcass this the following functions must be implemented:

    1.  get_more_tasks: This function takes the page content as well as the
        scrape task params and returns a list of params defining what to scrape
        next.  The params must include 'endpoint' and 'task_type' which tell the
        generic scraper what endpoint we are getting and what we are doing
        with the endpoint when we do get it.
    2.  set_initial_vars:  This function should set any initial vars needed
        after a first scrape (session vars for example)
    3.  populate_data:  This function is called whenever a task loads a page
        that has important data in it.
"""

import abc
import logging

import dateparser
from lxml import html
from lxml.etree import XMLSyntaxError  # pylint:disable=no-name-in-module

from recidiviz.common.ingest_metadata import IngestMetadata
from recidiviz.persistence import persistence
from recidiviz.ingest import constants, ingest_utils
from recidiviz.ingest.models.ingest_info import IngestInfo
from recidiviz.ingest.scraper import Scraper


class BaseScraper(Scraper):
    """Generic class for scrapers."""

    def __init__(self, region_name):
        super(BaseScraper, self).__init__(region_name)

    def get_initial_task(self):
        """
        Get the name of the first task to be run.  For generic scraper we always
        call into the same function.

        Returns:
            The name of the task to run first.

        """
        return '_generic_scrape'

    # Each scraper can override this, by default it is treated as a url endpoint
    # but any scraper can override this and treat it as a different type of
    # endpoint like an API endpoint for example.
    def _fetch_content(self, endpoint, post_data=None, json_data=None):
        """Returns the page content.

        Args:
            endpoint: the endpoint to make a request to.
            post_data: dict of parameters to pass into the html request.

        Returns:
            Returns the content of the page or -1.
        """
        logging.info('Fetching content with endpoint: %s', endpoint)
        page = self.fetch_page(endpoint,
                               post_data=post_data, json_data=json_data)
        if page == -1:
            return -1
        try:
            html_tree = html.fromstring(page.content)
        except XMLSyntaxError as e:
            logging.error("Error parsing initial page. Error: %s\nPage:\n\n%s",
                          e, page.content)
            return -1
        return html_tree

    def _generic_scrape(self, params):
        """
        General handler for all scrape tasks.  This function is a generic entry
        point into all types of scrapes.  It decides what to call based on
        params.

        Args:
            params: dict of parameters passed from the last scrape session.

        Returns:
            Nothing if successful, -1 if it fails
        """
        # If this is called without any params set, we assume its the
        # first call
        task_type = params.get('task_type', self.get_initial_task_type())
        endpoint = params.get('endpoint', self.get_initial_endpoint() if
                              self.is_initial_task(task_type) else None)

        # Here we handle a special case where we weren't really sure
        # we were going to get data when we submitted a task, but then
        # we ended up with data, so no more requests are required,
        # just the content we already have.
        if endpoint is None:
            content = html.fromstring(params.get('content'))
        else:
            post_data = params.get(
                'post_data',
                self.get_initial_data() if self.is_initial_task(task_type)
                else None)
            json_data = params.get('json', None)

            # Let the child transform the post_data if it wants before
            # sending the requests.  This hook is in here in case the
            # child did something like compress the post_data before
            # it put it on the queue.
            self.transform_post_data(post_data)

            # We always fetch some content before doing anything.
            # Note that we use get here for the post_data to return a
            # default value of None if this scraper doesn't set it.
            content = self._fetch_content(endpoint, post_data=post_data,
                                          json_data=json_data)
            if content == -1:
                return -1

        ingest_info = None

        # For an initial task we just make a call to get initial variables.
        # This may be a noop for some scrapers.
        if self.is_initial_task(task_type):
            logging.info('Initial task for %s', self.region.region_code)
            self.set_initial_vars(content, params)
        if self.should_scrape_data(task_type):
            # If we want to scrape data, we should either create an ingest_info
            # object or get the one that already exists.
            logging.info('Scraping data for %s and endpoint: %s',
                         self.region.region_code, endpoint)
            ingest_info = params.get('ingest_info', IngestInfo())
            ingest_info = self.populate_data(content, params, ingest_info)
        if self.should_get_more_tasks(task_type):
            logging.info('Getting more tasks for %s and endpoint: %s',
                         self.region.region_code, endpoint)
            tasks = self.get_more_tasks(content, params)
            for task_params in tasks:
                # Always pass along the scrape type as well.
                task_params['scrape_type'] = params['scrape_type']
                task_params['scraper_start_time'] = params['scraper_start_time']
                # If we have an ingest info to work with, we need to pass that
                # along as well.
                if ingest_info:
                    task_params['ingest_info'] = ingest_info
                self.add_task('_generic_scrape', task_params)
        # If we don't have any more tasks to scrape, we are at a leaf node and
        # we are ready to populate the database.
        else:
            # Something is wrong if we get here but don't have ingest_info
            # to work with.
            if not ingest_info:
                raise ValueError(
                    'IngestInfo must be populated if there are no more tasks')
            scraper_start_time = dateparser.parse(params['scraper_start_time'])
            logging.info('Writing the ingest_info to the database for %s:',
                         self.region.region_code)
            logging.info(ingest_info)
            logging.info('Last seen time of person being set as: %s',
                         scraper_start_time)
            metadata = IngestMetadata(self.region.region_code,
                                      scraper_start_time)
            persistence.write(
                ingest_utils.convert_ingest_info_to_proto(
                    ingest_info), metadata)
        return None

    def is_initial_task(self, task_type):
        """Tells us if the task_type is initial task_type.

        Args:
            A hexcode representing the task_type

        Returns:
            boolean representing whether or not the task_type is initial task.
        """
        return task_type & constants.INITIAL_TASK

    def should_get_more_tasks(self, task_type):
        """Tells us if we should get more tasks.

        Args:
            A hexcode representing the task_type

        Returns:
            boolean whether or not we should get more tasks
        """
        return task_type & constants.GET_MORE_TASKS

    def should_scrape_data(self, task_type):
        """Tells us if we should scrape data from a page.

        Args:
            A hexcode representing the task_type

        Returns:
            boolean whether or not we should scrape a person from the page.
        """
        return task_type & constants.SCRAPE_DATA

    @abc.abstractmethod
    def get_more_tasks(self, content, params):
        """
        Gets more tasks based on the content and params passed in.  This
        function should determine which task params, if any, should be
        added to the queue

        Every scraper must implement this.  It should return a list of params
        Where each param corresponds to a task we want to run.
        Mandatory fields are endpoint and task_type.

        Args:
            content: An lxml html tree.
            params: dict of parameters passed from the last scrape session.

        Returns:
            A list of dicts each containing endpoint and task_type at minimum
            which tell the generic scraper how to behave.
        """

    def set_initial_vars(self, content, params):
        """
        Sets initial vars in the params that it will pass on to future scrapes

        Args:
            content: An lxml html tree.
            params: dict of parameters passed from the last scrape session.
        """

    @abc.abstractmethod
    def populate_data(self, content, params, ingest_info):
        """
        Populates the ingest info object from the content and params given

        Args:
            content: An lxml html tree.
            params: dict of parameters passed from the last scrape session.
            ingest_info: The IngestInfo object to populate
        """

    def transform_post_data(self, data):
        """If the child needs to transform the data in any way before it sends
        the request, it can override this function.

        Args:
            data: dict of parameters to send as data to the post request.
        """

    def get_initial_endpoint(self):
        """Returns the initial endpoint to hit on the first call
        Returns:
            A string representing the initial endpoint to hit
        """
        return self.get_region().base_url

    def get_initial_data(self):
        """Returns the initial data to send on the first call
        Returns:
            A dict of data to send in the request.
        """
        return None

    def get_initial_task_type(self):
        """Returns the initial task_type to set on the first call
        Returns:
            A hex code from constants telling us how to behave on the first call
        """
        return constants.INITIAL_TASK_AND_MORE
