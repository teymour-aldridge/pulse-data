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
"""Dashboard dataset configuration."""

# Where the actual, final dashboard views live
DASHBOARD_VIEWS_DATASET: str = 'dashboard_views'

# Where the metrics that Dataflow jobs produce live
DATAFLOW_METRICS_DATASET: str = 'dataflow_metrics'

# Where static reference tables live
REFERENCE_TABLES_DATASET: str = 'reference_tables'

# Where the base tables for the state schema live
STATE_BASE_DATASET: str = 'state'
