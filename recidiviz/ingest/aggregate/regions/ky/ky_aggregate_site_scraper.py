# Recidiviz - a data platform for criminal justice reform
# Copyright (C) 2019 Recidiviz, Inc.
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

"""Scrapes the kentucky aggregate site and finds pdfs to download."""
import datetime
from typing import Set
from urllib.parse import unquote
from lxml import html
import requests

from recidiviz.ingest.aggregate.regions.ky.ky_aggregate_ingest import parse_date

STATE_AGGREGATE_URL = ('https://corrections.ky.gov/About/researchandstats/'
                       'Pages/WeeklyJail.aspx')
BASE_URL = 'https://corrections.ky.gov{}'
ACCEPTABLE_DATE = datetime.date(year=2018, month=8, day=9)


def get_urls_to_download() -> Set[str]:
    page = requests.get(STATE_AGGREGATE_URL).text
    html_tree = html.fromstring(page)
    links = html_tree.xpath('//a/@href')

    aggregate_report_urls = set()
    for link in links:
        link = unquote(link)
        if 'weekly jail' in link.lower() and 'pdf' in link.lower():
            # Fix typo in link for ​​October 17, 2019
            if link.endswith('12-17-19.pdf'):
                link = link.replace('12-17-19', '10-17-19')
            elif link.endswith('09-26-19.pdf'):
                link = link.replace('09-26-19.pdf', '09-26-19 new.pdf')
            else:
                # Make sure we only take things after Aug 9th 2018 as the format
                # changed before that.
                d = parse_date(link)
                if d < ACCEPTABLE_DATE:
                    continue
            url = BASE_URL.format(link)
            aggregate_report_urls.add(url)
    return aggregate_report_urls
