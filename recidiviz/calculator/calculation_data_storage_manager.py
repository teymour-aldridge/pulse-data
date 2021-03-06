# Recidiviz - a data platform for criminal justice reform
# Copyright (C) 2020 Recidiviz, Inc.
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
"""Manages the storage of data produced by calculations."""
import logging
from http import HTTPStatus

import flask

from recidiviz.big_query.big_query_client import BigQueryClientImpl
from recidiviz.calculator.calculation_data_storage_config import DATAFLOW_METRICS_COLD_STORAGE_DATASET, \
    MAX_DAYS_IN_DATAFLOW_METRICS_TABLE
from recidiviz.calculator.query.state.dataset_config import DATAFLOW_METRICS_DATASET
from recidiviz.utils.auth import authenticate_request

calculation_data_storage_manager_blueprint = flask.Blueprint('calculation_data_storage_manager', __name__)


@calculation_data_storage_manager_blueprint.route('/prune_old_dataflow_data')
@authenticate_request
def prune_old_dataflow_data():
    """Calls the move_old_dataflow_metrics_to_cold_storage function."""
    move_old_dataflow_metrics_to_cold_storage()

    return '', HTTPStatus.OK


def move_old_dataflow_metrics_to_cold_storage():
    """Moves old output in Dataflow metrics tables to tables in a cold storage dataset. We only keep the
    MAX_DAYS_IN_DATAFLOW_METRICS_TABLE days worth of data in a Dataflow metric table at once. All other
    output is moved to cold storage.
    """
    bq_client = BigQueryClientImpl()
    dataflow_metrics_dataset = DATAFLOW_METRICS_DATASET
    cold_storage_dataset = DATAFLOW_METRICS_COLD_STORAGE_DATASET
    dataflow_metrics_tables = bq_client.list_tables(dataflow_metrics_dataset)

    for table_ref in dataflow_metrics_tables:
        table_id = table_ref.table_id

        filter_clause = """WHERE created_on NOT IN
                          (SELECT DISTINCT created_on FROM `{project_id}.{dataflow_metrics_dataset}.{table_id}` 
                          ORDER BY created_on DESC
                          LIMIT {day_count_limit})""".format(
                              project_id=table_ref.project,
                              dataflow_metrics_dataset=table_ref.dataset_id,
                              table_id=table_ref.table_id,
                              day_count_limit=MAX_DAYS_IN_DATAFLOW_METRICS_TABLE
                          )

        cold_storage_dataset_ref = bq_client.dataset_ref_for_id(cold_storage_dataset)

        if bq_client.table_exists(cold_storage_dataset_ref, table_id):
            # Move data from the Dataflow metrics dataset into the cold storage dataset
            insert_job = bq_client.insert_into_table_from_table_async(source_dataset_id=dataflow_metrics_dataset,
                                                                      source_table_id=table_id,
                                                                      destination_dataset_id=cold_storage_dataset,
                                                                      destination_table_id=table_id,
                                                                      source_data_filter_clause=filter_clause,
                                                                      allow_field_additions=True)

            # Wait for the insert job to complete before running the delete job
            insert_job.result()
        else:
            # This table doesn't yet exist in cold storage. Create it.
            table_query = f"SELECT * FROM `{bq_client.project_id}.{dataflow_metrics_dataset}.{table_id}` " \
                          f"{filter_clause}"

            create_job = bq_client.create_table_from_query_async(cold_storage_dataset,
                                                                 table_id,
                                                                 table_query,
                                                                 query_parameters=[])

            # Wait for the create job to complete before running the delete job
            create_job.result()

        # Delete that data from the Dataflow dataset
        delete_job = bq_client.delete_from_table_async(dataflow_metrics_dataset, table_ref.table_id, filter_clause)

        # Wait for the delete job to complete before moving on
        delete_job.result()


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)

    move_old_dataflow_metrics_to_cold_storage()
