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
"""Event Based Supervision by Person."""
# pylint: disable=trailing-whitespace, line-too-long

from recidiviz.big_query.big_query_view import BigQueryView
from recidiviz.calculator.query import bq_utils
from recidiviz.calculator.query.state import dataset_config

EVENT_BASED_SUPERVISION_VIEW_NAME = 'event_based_supervision_populations'

EVENT_BASED_SUPERVISION_DESCRIPTION = """
 Supervision data on the person level with demographic information

 Expanded Dimensions: district, supervision_type
 """

EVENT_BASED_SUPERVISION_QUERY_TEMPLATE = \
    """
    /*{description}*/
    SELECT
      person_id, state_code, year, month,
      supervision_type,
      district,
      supervising_officer_external_id AS officer_external_id,
      gender, age_bucket, race, ethnicity, assessment_score_bucket
    FROM `{project_id}.{metrics_dataset}.supervision_population_metrics`
    JOIN `{project_id}.{reference_dataset}.most_recent_job_id_by_metric_and_state_code` job
        USING (state_code, job_id, year, month, metric_period_months),
    {district_dimension},
    {supervision_dimension}
    WHERE methodology = 'EVENT'
      AND metric_period_months = 1
      AND person_id IS NOT NULL
      AND district IS NOT NULL
      AND month IS NOT NULL
      AND year >= EXTRACT(YEAR FROM DATE_SUB(CURRENT_DATE(), INTERVAL 3 YEAR))
      AND job.metric_type = 'SUPERVISION_POPULATION'
    """

EVENT_BASED_SUPERVISION_VIEW = BigQueryView(
    dataset_id=dataset_config.REFERENCE_TABLES_DATASET,
    view_id=EVENT_BASED_SUPERVISION_VIEW_NAME,
    view_query_template=EVENT_BASED_SUPERVISION_QUERY_TEMPLATE,
    description=EVENT_BASED_SUPERVISION_DESCRIPTION,
    metrics_dataset=dataset_config.DATAFLOW_METRICS_DATASET,
    reference_dataset=dataset_config.REFERENCE_TABLES_DATASET,
    district_dimension=bq_utils.unnest_district(),
    supervision_dimension=bq_utils.unnest_supervision_type(),
)

if __name__ == '__main__':
    print(EVENT_BASED_SUPERVISION_VIEW.view_id)
    print(EVENT_BASED_SUPERVISION_VIEW.view_query)
