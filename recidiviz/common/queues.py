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

"""Cloud Tasks queue helper functions."""

import datetime
import json
import logging
import uuid
from typing import List, Dict

from google.api_core import datetime_helpers
from google.cloud import exceptions, tasks
from google.protobuf import timestamp_pb2

from recidiviz.common.common_utils import retry_grpc
from recidiviz.ingest.direct.controllers.direct_ingest_types import \
    IngestArgs
from recidiviz.utils import environment, metadata
from recidiviz.utils.regions import Region

NUM_GRPC_RETRIES = 2

_client = None


def client():
    global _client
    if not _client:
        _client = tasks.CloudTasksClient()
    return _client


@environment.test_only
def clear_client():
    global _client
    _client = None


def format_queue_path(queue_name):
    """Formats a queue name into its full Cloud Tasks queue path.

    Args:
        queue_name: `str` queue name.
    Returns:
        A Cloud Tasks queue path string.
    """
    full_queue_path = client().queue_path(
        metadata.project_id(),
        metadata.region(),
        queue_name)

    return full_queue_path


def format_task_path(queue_name: str, task_name: str):
    """Creates a task path out of the necessary parts.

    Task path is of the form:
        '/projects/{project}/locations/{location}'
        '/queues/{queue}/tasks/{task_name}'
    """
    return client().task_path(
        metadata.project_id(),
        metadata.region(),
        queue_name,
        task_name)


def format_scrape_task_path(queue_name: str, region_code: str, task_id: str):
    """Creates a scrape task path out of the necessary parts.

    Task path is of the form:
        '/projects/{project}/locations/{location}'
        '/queues/{queue}/tasks/{region_code}-{task_id}'
    """
    return format_task_path(queue_name, '{}-{}'.format(region_code, task_id))


def purge_scrape_tasks(*, region_code: str, queue_name: str):
    """Purge scrape tasks for a given region from its queue.

    Args:
        region_code: `str` region code.
        queue_name: `str` queue name.
    """
    for task in list_scrape_tasks(
            region_code=region_code, queue_name=queue_name):
        try:
            retry_grpc(NUM_GRPC_RETRIES, client().delete_task, task.name)
        except exceptions.NotFound as e:
            logging.debug('Task not found: [%s]', e)


def list_scrape_tasks(*, region_code: str, queue_name: str) \
        -> List[tasks.types.Task]:
    """List scrape tasks for the given region and queue"""
    region_task_prefix = format_scrape_task_path(queue_name, region_code, '')
    return [task for task in retry_grpc(NUM_GRPC_RETRIES,
                                        client().list_tasks,
                                        format_queue_path(queue_name))
            if task.name.startswith(region_task_prefix)]


def create_scrape_task(*, region_code, queue_name, url, body):
    """Create a scrape task in a queue.

    Args:
        region_code: `str` region code.
        queue_name: `str` queue name.
        url: `str` App Engine worker url.
        body: `dict` task body to be passed to worker.
    """
    task = tasks.types.Task(
        name=format_scrape_task_path(queue_name, region_code, uuid.uuid4()),
        app_engine_http_request={
            'relative_uri': url,
            'body': json.dumps(body).encode()
        }
    )

    retry_grpc(NUM_GRPC_RETRIES,
               client().create_task,
               format_queue_path(queue_name),
               task)


SCRAPER_PHASE_QUEUE = 'scraper-phase'


def enqueue_scraper_phase(*, region_code, url):
    """Add a task to trigger the next phase of a scrape.

    This triggers the phase at the given url for an individual region, passing
    the `region_code` as a url parameter. For example, this can trigger stopping
    a scraper or inferring release for a particular region.
    """
    task = tasks.types.Task(
        app_engine_http_request={
            'http_method': 'GET',
            'relative_uri': '{url}?region={region_code}'.format(
                url=url, region_code=region_code
            ),
        }
    )
    retry_grpc(
        NUM_GRPC_RETRIES,
        client().create_task,
        format_queue_path(SCRAPER_PHASE_QUEUE),
        task
    )


BIGQUERY_QUEUE = 'bigquery'


def create_bq_task(table_name: str, module: str, url: str):
    """Create a BigQuery table export path.

    Args:
        table_name: Cloud SQL table to export to BQ. Must be defined in
            the *_TABLES_TO_EXPORT for the given schema.
        module: The module of the table being exported, either 'county' or
            'state'.
        url: App Engine worker URL.
    """
    body = {'table_name': table_name, 'module': module}
    task_id = '{}-{}-{}-{}'.format(
        table_name, module, str(datetime.date.today()), uuid.uuid4())
    task_name = format_task_path(BIGQUERY_QUEUE, task_id)
    task = tasks.types.Task(
        name=task_name,
        app_engine_http_request={
            'relative_uri': url,
            'body': json.dumps(body).encode()
        }
    )
    retry_grpc(
        NUM_GRPC_RETRIES,
        client().create_task,
        format_queue_path(BIGQUERY_QUEUE),
        task
    )


DIRECT_INGEST_SCHEDULER_QUEUE = 'direct-ingest-scheduler'


def _get_body_from_args(ingest_args: IngestArgs) -> Dict:
    body = {
        'ingest_args': ingest_args.to_serializable(),
        'args_type': ingest_args.__class__.__name__
    }
    return body


def create_direct_ingest_scheduler_queue_task(
        region: Region, ingest_args: IngestArgs, delay_sec: int):
    """Creates a scheduler task for direct ingest for a given region. Scheduler
    tasks should be short-running and queue process_job tasks if there is more
    work to do.

    Args:
        region: `Region` direct ingest region.
        ingest_args: `IngestArgs` args for the current direct ingest task.
        delay_sec: `int` the number of seconds to wait before the next task.
    """
    body = _get_body_from_args(ingest_args)
    schedule_time = datetime.datetime.now() + \
                    datetime.timedelta(seconds=delay_sec)

    schedule_time_sec = datetime_helpers.to_milliseconds(schedule_time) // 1000
    schedule_timestamp = timestamp_pb2.Timestamp(seconds=schedule_time_sec)

    task_id = '{}-{}-{}'.format(
        region.region_code, str(datetime.date.today()), uuid.uuid4())
    task_name = format_task_path(DIRECT_INGEST_SCHEDULER_QUEUE, task_id)
    task = tasks.types.Task(
        name=task_name,
        schedule_time=schedule_timestamp,
        app_engine_http_request={
            'relative_uri': f'/direct/scheduler?region={region.region_code}',
            'body': json.dumps(body).encode()
        }
    )
    retry_grpc(
        NUM_GRPC_RETRIES,
        client().create_task,
        format_queue_path(DIRECT_INGEST_SCHEDULER_QUEUE),
        task
    )


DIRECT_INGEST_STATE_TASK_QUEUE = 'direct-ingest-state-task-queue'
DIRECT_INGEST_JAILS_TASK_QUEUE = 'direct-ingest-jpp-task-queue'


def create_direct_ingest_process_job_task(region: Region,
                                          ingest_args: IngestArgs):
    """Queues a direct ingest process job task. All direct ingest data
    processing should happen through this endpoint.
    Args:
        region: `Region` direct ingest region.
        ingest_args: `IngestArgs` args for the current direct ingest task.
    """
    body = _get_body_from_args(ingest_args)
    task_id = '{}-{}-{}'.format(
        region.region_code, str(datetime.date.today()), uuid.uuid4())
    task_name = format_task_path(region.get_queue_name(), task_id)
    task = tasks.types.Task(
        name=task_name,
        app_engine_http_request={
            'relative_uri':
                f'/direct/process_job?region={region.region_code}',
            'body': json.dumps(body).encode()
        }
    )
    retry_grpc(
        NUM_GRPC_RETRIES,
        client().create_task,
        format_queue_path(region.get_queue_name()),
        task
    )
