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
"""Runs the supervision calculation pipeline.

usage: pipeline.py --input=INPUT_LOCATION --output=OUTPUT_LOCATION
                    --project=PROJECT --reference_input=REFERENCE_LOCATION
                    --methodology=METHODOLOGY
                    [--include_age] [--include_gender]
                    [--include_race] [--include_ethnicity]
                    [--metric_type] [--state_code]

Example output to GCP storage bucket:
python -m recidiviz.calculator.supervision.pipeline
        --project=recidiviz-project-name
        --input=recidiviz-project-name.dataset
        --reference_input=recidiviz-project-name.ref_dataset
        --output=gs://recidiviz-bucket/output_location
            --methodology=BOTH --metric_type='POPULATION'
            --state_code='US_ND'

Example output to local file:
python -m recidiviz.calculator.supervision.pipeline
        --project=recidiviz-project-name
        --input=recidiviz-project-name.dataset
        --reference_input=recidiviz-project-name.ref_dataset
        --output=output_file --methodology=PERSON

Example output including race and gender dimensions:
python -m recidiviz.calculator.supervision.pipeline
        --project=recidiviz-project-name
        --input=recidiviz-project-name.dataset
        --reference_input=recidiviz-project-name.ref_dataset
        --output=output_file --methodology=EVENT
            --include_race=True --include_gender=True

"""
import argparse
import datetime
import json
import logging
from typing import Dict, Any, List, Tuple

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions
from apache_beam.pvalue import AsDict
from apache_beam.typehints import with_input_types, with_output_types
from more_itertools import one

from recidiviz.calculator.pipeline.supervision import identifier, calculator
from recidiviz.calculator.pipeline.supervision.metrics import \
    SupervisionMetric, SupervisionPopulationMetric, \
    SupervisionRevocationMetric, SupervisionSuccessMetric, \
    TerminatedSupervisionAssessmentScoreChangeMetric, \
    SupervisionRevocationAnalysisMetric, SupervisionRevocationViolationTypeAnalysisMetric
from recidiviz.calculator.pipeline.supervision.metrics import \
    SupervisionMetricType as MetricType
from recidiviz.calculator.pipeline.supervision.supervision_time_bucket import \
    SupervisionTimeBucket
from recidiviz.calculator.pipeline.utils.beam_utils import SumFn, \
    SupervisionSuccessFn, ConvertDictToKVTuple,\
    TerminatedSupervisionAssessmentScoreChange
from recidiviz.calculator.pipeline.utils.entity_hydration_utils import \
    SetViolationResponseOnIncarcerationPeriod, SetViolationOnViolationsResponse
from recidiviz.calculator.pipeline.utils.execution_utils import get_job_id
from recidiviz.calculator.pipeline.utils.extractor_utils import BuildRootEntity
from recidiviz.calculator.pipeline.utils.metric_utils import \
    json_serializable_metric_key, MetricMethodologyType
from recidiviz.persistence.database.schema.state import schema
from recidiviz.persistence.entity.state import entities
from recidiviz.utils import environment

# Cached job_id value
_job_id = None


def job_id(pipeline_options: Dict[str, str]) -> str:
    global _job_id
    if not _job_id:
        _job_id = get_job_id(pipeline_options)
    return _job_id


@environment.test_only
def clear_job_id():
    global _job_id
    _job_id = None


@with_input_types(beam.typehints.Tuple[entities.StatePerson, List[SupervisionTimeBucket]])
@with_output_types(SupervisionMetric)
class GetSupervisionMetrics(beam.PTransform):
    """Transforms a StatePerson and their SupervisionTimeBuckets into SupervisionMetrics."""
    def __init__(self, pipeline_options: Dict[str, str],
                 inclusions: Dict[str, bool],
                 metric_type: str):
        super(GetSupervisionMetrics, self).__init__()
        self._pipeline_options = pipeline_options
        self.inclusions = inclusions

        for metric_option in MetricType:
            if metric_type in ('ALL', metric_option.value):
                self.inclusions[metric_option.value] = True
                logging.info("Producing %s metrics", metric_option.value)
            else:
                self.inclusions[metric_option.value] = False

    def expand(self, input_or_inputs):
        # Calculate supervision metric combinations from a StatePerson and their
        # SupervisionTimeBuckets
        supervision_metric_combinations = (
            input_or_inputs | 'Map to metric combinations' >>
            beam.ParDo(CalculateSupervisionMetricCombinations(),
                       **self.inclusions).with_outputs('populations',
                                                       'revocations',
                                                       'successes',
                                                       'assessment_changes',
                                                       'revocation_analyses',
                                                       'revocation_violation_type_analyses'))

        # Calculate the supervision population values for the metrics combined by key
        populations_with_sums = (supervision_metric_combinations.populations
                                 | 'Calculate supervision population values' >>
                                 beam.CombinePerKey(SumFn()))

        # Calculate the revocation count values for metrics combined by key
        revocations_with_sums = (supervision_metric_combinations.revocations
                                 | 'Calculate supervision revocation values' >>
                                 beam.CombinePerKey(SumFn()))

        # Calculate the supervision success values for the metrics combined by key
        successes_with_sums = (supervision_metric_combinations.successes
                               | 'Calculate the supervision success values' >>
                               beam.CombinePerKey(SupervisionSuccessFn()))

        # Calculate the assessment score changes for the metrics combined by key
        assessment_changes_with_averages = (
            supervision_metric_combinations.assessment_changes
            | 'Calculate the assessment score change average values' >>
            beam.CombinePerKey(TerminatedSupervisionAssessmentScoreChange())
        )

        # Calculate the revocation analyses count values for metrics combined
        # by key
        revocation_analyses_with_sums = (
            supervision_metric_combinations.revocation_analyses
            | 'Calculate the revocation analyses count values' >>
            beam.CombinePerKey(SumFn())
        )

        # Calculate the violation type analyses count values for metrics combined by key
        revocation_violation_type_analyses_with_sums = (
            supervision_metric_combinations.revocation_violation_type_analyses
            | 'Calculate the revocation violation type analyses count values' >>
            beam.CombinePerKey(SumFn())
        )

        # Produce the SupervisionPopulationMetrics
        population_metrics = (populations_with_sums | 'Produce supervision population metrics' >>
                              beam.ParDo(ProduceSupervisionMetrics(), **self._pipeline_options))

        # Produce the SupervisionRevocationMetrics
        revocation_metrics = (revocations_with_sums | 'Produce supervision revocation metrics' >>
                              beam.ParDo(ProduceSupervisionMetrics(), **self._pipeline_options))

        # Produce the SupervisionSuccessMetrics
        success_metrics = (successes_with_sums | 'Produce supervision success metrics' >>
                           beam.ParDo(ProduceSupervisionMetrics(), **self._pipeline_options))

        # Produce the TerminatedSupervisionAssessmentScoreChangeMetrics
        assessment_change_metrics = (assessment_changes_with_averages | 'Produce assessment score change metrics' >>
                                     beam.ParDo(ProduceSupervisionMetrics(), **self._pipeline_options))

        # Produce the SupervisionRevocationAnalysisMetrics
        revocation_analysis_metrics = (revocation_analyses_with_sums |
                                       'Produce supervision revocation analysis metrics' >>
                                       beam.ParDo(ProduceSupervisionMetrics(), **self._pipeline_options))

        # Produce the SupervisionRevocationViolationTypeAnalysisMetrics
        revocation_violation_type_analysis_metrics = (revocation_violation_type_analyses_with_sums
                                                      | 'Produce revocation violation type analysis metrics' >>
                                                      beam.ParDo(ProduceSupervisionMetrics(), **self._pipeline_options))

        # Merge the metric groups
        merged_metrics = ((population_metrics, revocation_metrics,
                           success_metrics, assessment_change_metrics,
                           revocation_analysis_metrics, revocation_violation_type_analysis_metrics)
                          | 'Merge population, revocation, success,'
                            ' revocation analysis, revocation violation type analysis metrics and'
                            ' assessment change metrics' >>
                          beam.Flatten())

        # Return SupervisionMetrics objects
        return merged_metrics


@with_input_types(beam.typehints.Tuple[int, Dict[str, Any]],
                  beam.typehints.Optional[Dict[Any, Tuple[Any, Dict[str, Any]]]],
                  beam.typehints.Optional[Dict[Any, Tuple[Any, Dict[str, Any]]]])
@with_output_types(beam.typehints.Tuple[entities.StatePerson, List[SupervisionTimeBucket]])
class ClassifySupervisionTimeBuckets(beam.DoFn):
    """Classifies time on supervision as years and months with or without revocation, and classifies months of
    projected completion as either successful or not."""

    #pylint: disable=arguments-differ
    def process(self, element, ssvr_agent_associations, supervision_period_to_agent_associations, **kwargs):
        """Identifies instances of revocation and non-revocation time buckets on supervision, and classifies months of
        projected completion as either successful or not.
        """
        state_code = 'ALL'

        if kwargs.get('state_code') is not None:
            state_code = kwargs.get('state_code')
            logging.info("Limiting calculations to state: %s", state_code)

        _, person_periods = element

        # Get the StateSupervisionSentences as a list
        supervision_sentences = list(person_periods['supervision_sentences'])

        # Get the StateSupervisionPeriods as a list
        supervision_periods = list(person_periods['supervision_periods'])

        # Get the StateIncarcerationPeriods as a list
        incarceration_periods = list(person_periods['incarceration_periods'])

        # Get the StateAssessments as a list
        assessments = list(person_periods['assessments'])

        # Get the StateSupervisionViolationResponses as a list
        violation_responses = list(person_periods['violation_responses'])

        # Get the StatePerson
        person = one(person_periods['person'])

        # Find the SupervisionTimeBuckets from the supervision and incarceration
        # periods
        supervision_time_buckets = identifier.find_supervision_time_buckets(
            supervision_sentences,
            supervision_periods,
            incarceration_periods,
            assessments,
            violation_responses,
            ssvr_agent_associations,
            supervision_period_to_agent_associations,
            state_code)

        if not supervision_time_buckets:
            logging.info("No valid supervision time buckets for person with"
                         "id: %d. Excluding them from the "
                         "calculations.", person.person_id)
        else:
            yield (person, supervision_time_buckets)

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


@with_input_types(beam.typehints.Tuple[entities.StatePerson,
                                       List[SupervisionTimeBucket]])
@with_output_types(beam.typehints.Tuple[str, Any])
class CalculateSupervisionMetricCombinations(beam.DoFn):
    """Calculates supervision metric combinations."""

    def process(self, element, *args, **kwargs):
        """Produces various supervision metric combinations.

        Sends the calculator the StatePerson entity and their corresponding SupervisionTimeBuckets for mapping all
        supervision combinations.

        Args:
            element: Tuple containing a StatePerson and their SupervisionTimeBuckets
            **kwargs: This should be a dictionary with values for the following keys:
                    - age_bucket
                    - gender
                    - race
                    - ethnicity
        Yields:
            Each supervision metric combination, tagged by metric type.
        """
        person, supervision_time_buckets = element

        # Calculate supervision metric combinations for this person and their supervision time buckets
        metric_combinations = calculator.map_supervision_combinations(person,
                                                                      supervision_time_buckets,
                                                                      kwargs)

        # Return each of the supervision metric combinations
        for metric_combination in metric_combinations:
            metric_key, value = metric_combination
            metric_type = metric_key.get('metric_type')

            # Converting the metric key to a JSON string so it is hashable
            serializable_dict = json_serializable_metric_key(metric_key)
            json_key = json.dumps(serializable_dict, sort_keys=True)

            if metric_type == MetricType.POPULATION.value:
                yield beam.pvalue.TaggedOutput('populations', (json_key, value))
            elif metric_type == MetricType.REVOCATION.value:
                yield beam.pvalue.TaggedOutput('revocations', (json_key, value))
            elif metric_type == MetricType.SUCCESS.value:
                yield beam.pvalue.TaggedOutput('successes', (json_key, value))
            elif metric_type == MetricType.ASSESSMENT_CHANGE.value:
                yield beam.pvalue.TaggedOutput('assessment_changes', (json_key, value))
            elif metric_type == MetricType.REVOCATION_ANALYSIS.value:
                yield beam.pvalue.TaggedOutput('revocation_analyses', (json_key, value))
            elif metric_type == MetricType.REVOCATION_VIOLATION_TYPE_ANALYSIS.value:
                yield beam.pvalue.TaggedOutput('revocation_violation_type_analyses', (json_key, value))

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


@with_input_types(beam.typehints.Tuple[str, int], **{'runner': str,
                                                     'project': str,
                                                     'job_name': str,
                                                     'region': str,
                                                     'job_timestamp': str})
@with_output_types(SupervisionMetric)
class ProduceSupervisionMetrics(beam.DoFn):
    """Produces SupervisionPopulationMetrics and SupervisionRevocationMetrics ready for persistence."""
    def process(self, element, *args, **kwargs):
        pipeline_options = kwargs

        pipeline_job_id = job_id(pipeline_options)

        (metric_key, value) = element

        if value is None:
            # Due to how the pipeline arrives at this function, this should be impossible.
            raise ValueError("No value associated with this metric key.")

        # Convert JSON string to dictionary
        dict_metric_key = json.loads(metric_key)
        metric_type = dict_metric_key.get('metric_type')

        if metric_type == MetricType.POPULATION.value:
            dict_metric_key['count'] = value

            supervision_metric = SupervisionPopulationMetric.build_from_metric_key_group(
                dict_metric_key, pipeline_job_id)
        elif metric_type == MetricType.REVOCATION.value:
            dict_metric_key['count'] = value

            supervision_metric = SupervisionRevocationMetric.build_from_metric_key_group(
                dict_metric_key, pipeline_job_id)
        elif metric_type == MetricType.SUCCESS.value:
            dict_metric_key['successful_completion_count'] = value.get('successful_completion_count')
            dict_metric_key['projected_completion_count'] = value.get('projected_completion_count')

            supervision_metric = SupervisionSuccessMetric.build_from_metric_key_group(
                dict_metric_key, pipeline_job_id)
        elif metric_type == MetricType.ASSESSMENT_CHANGE.value:
            dict_metric_key['count'] = value.get('count')

            dict_metric_key['average_score_change'] = value.get('average_score_change')

            supervision_metric = TerminatedSupervisionAssessmentScoreChangeMetric.build_from_metric_key_group(
                dict_metric_key, pipeline_job_id
            )
        elif metric_type == MetricType.REVOCATION_ANALYSIS.value:
            dict_metric_key['count'] = value

            supervision_metric = SupervisionRevocationAnalysisMetric.build_from_metric_key_group(
                dict_metric_key, pipeline_job_id
            )
        elif metric_type == MetricType.REVOCATION_VIOLATION_TYPE_ANALYSIS.value:
            dict_metric_key['count'] = value

            supervision_metric = SupervisionRevocationViolationTypeAnalysisMetric.build_from_metric_key_group(
                dict_metric_key, pipeline_job_id
            )
        else:
            logging.error("Unexpected metric of type: %s",
                          dict_metric_key.get('metric_type'))
            return

        if supervision_metric:
            yield supervision_metric

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


@with_input_types(SupervisionMetric)
@with_output_types(beam.typehints.Dict[str, Any])
class SupervisionMetricWritableDict(beam.DoFn):
    """Builds a dictionary in the format necessary to write the output to BigQuery."""

    def process(self, element, *args, **kwargs):
        """The beam.io.WriteToBigQuery transform requires elements to be in dictionary form, where the values are in
        formats as required by BigQuery I/O connector.

        For a list of required formats, see the "Data types" section of:
            https://beam.apache.org/documentation/io/built-in/google-bigquery/

        Args:
            element: A SupervisionMetric

        Yields:
            A dictionary representation of the SupervisionMetric in the format Dict[str, Any] so that it can be written
                to BigQuery using beam.io.WriteToBigQuery.
        """
        element_dict = json_serializable_metric_key(element.__dict__)

        if isinstance(element, SupervisionPopulationMetric):
            yield beam.pvalue.TaggedOutput('populations', element_dict)
        elif isinstance(element, SupervisionRevocationAnalysisMetric):
            yield beam.pvalue.TaggedOutput('revocation_analyses', element_dict)
        elif isinstance(element, SupervisionRevocationViolationTypeAnalysisMetric):
            yield beam.pvalue.TaggedOutput('revocation_violation_type_analyses', element_dict)
        elif isinstance(element, SupervisionRevocationMetric) \
                and not isinstance(element, SupervisionRevocationAnalysisMetric):
            yield beam.pvalue.TaggedOutput('revocations', element_dict)
        elif isinstance(element, SupervisionSuccessMetric):
            yield beam.pvalue.TaggedOutput('successes', element_dict)
        elif isinstance(element, TerminatedSupervisionAssessmentScoreChangeMetric):
            yield beam.pvalue.TaggedOutput('assessment_changes', element_dict)

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


def parse_arguments(argv):
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser()

    # Parse arguments
    parser.add_argument('--input',
                        dest='input',
                        type=str,
                        help='BigQuery dataset to query.',
                        required=True)

    parser.add_argument('--reference_input',
                        dest='reference_input',
                        type=str,
                        help='BigQuery reference dataset to query.',
                        required=True)

    # TODO: Generalize these arguments
    parser.add_argument('--include_age',
                        dest='include_age',
                        type=bool,
                        help='Include metrics broken down by age.',
                        default=False)

    parser.add_argument('--include_gender',
                        dest='include_gender',
                        type=bool,
                        help='Include metrics broken down by gender.',
                        default=False)

    parser.add_argument('--include_race',
                        dest='include_race',
                        type=bool,
                        help='Include metrics broken down by race.',
                        default=False)

    parser.add_argument('--include_ethnicity',
                        dest='include_ethnicity',
                        type=bool,
                        help='Include metrics broken down by ethnicity.',
                        default=False)

    parser.add_argument('--methodology',
                        dest='methodology',
                        type=str,
                        choices=['PERSON', 'EVENT', 'BOTH'],
                        help='PERSON, EVENT, or BOTH',
                        required=True)

    parser.add_argument('--output',
                        dest='output',
                        type=str,
                        help='Output dataset to write results to.',
                        required=True)

    parser.add_argument('--metric_type',
                        dest='metric_type',
                        type=str,
                        choices=[
                            'ALL',
                            MetricType.ASSESSMENT_CHANGE.value,
                            MetricType.POPULATION.value,
                            MetricType.REVOCATION.value,
                            MetricType.REVOCATION_ANALYSIS.value,
                            MetricType.REVOCATION_VIOLATION_TYPE_ANALYSIS.value,
                            MetricType.SUCCESS.value
                        ],
                        help='The type of metric to calculate.',
                        default='ALL')

    # NOTE: Must stay up to date to include all active states
    parser.add_argument('--state_code',
                        dest='state_code',
                        type=str,
                        choices=['ALL', 'US_MO', 'US_ND'],
                        help='The state_code to include in the calculations',
                        default='ALL')

    return parser.parse_known_args(argv)


def dimensions_and_methodologies(known_args) -> \
        Tuple[Dict[str, bool], List[MetricMethodologyType]]:

    filterable_dimensions_map = {
        'include_age': 'age_bucket',
        'include_ethnicity': 'ethnicity',
        'include_gender': 'gender',
        'include_race': 'race',
    }

    known_args_dict = vars(known_args)

    dimensions: Dict[str, bool] = {}

    for dimension_key in filterable_dimensions_map:
        if not known_args_dict[dimension_key]:
            dimensions[filterable_dimensions_map[dimension_key]] = False
        else:
            dimensions[filterable_dimensions_map[dimension_key]] = True

    methodologies = []

    if known_args.methodology == 'BOTH':
        methodologies.append(MetricMethodologyType.EVENT)
        methodologies.append(MetricMethodologyType.PERSON)
    else:
        methodologies.append(MetricMethodologyType[known_args.methodology])

    return dimensions, methodologies


def run(argv=None):
    """Runs the supervision calculation pipeline."""

    # Workaround to load SQLAlchemy objects at start of pipeline. This is necessary because the BuildRootEntity
    # function tries to access attributes of relationship properties on the SQLAlchemy room_schema_class before they
    # have been loaded. However, if *any* SQLAlchemy objects have been instantiated, then the relationship properties
    # are loaded and their attributes can be successfully accessed.
    _ = schema.StatePerson()

    # Parse command-line arguments
    known_args, pipeline_args = parse_arguments(argv)

    pipeline_options = PipelineOptions(pipeline_args)
    pipeline_options.view_as(SetupOptions).save_main_session = True

    # Get pipeline job details
    all_pipeline_options = pipeline_options.get_all_options()

    input_dataset = all_pipeline_options['project'] + '.' + known_args.input
    reference_dataset = all_pipeline_options['project'] + '.' + \
        known_args.reference_input

    with beam.Pipeline(argv=pipeline_args) as p:
        # Get StatePersons
        persons = (p | 'Load Persons' >> BuildRootEntity(dataset=input_dataset,
                                                         data_dict=None,
                                                         root_schema_class=schema.StatePerson,
                                                         root_entity_class=entities.StatePerson,
                                                         unifying_id_field='person_id',
                                                         build_related_entities=True))

        # Get StateIncarcerationPeriods
        incarceration_periods = (p | 'Load IncarcerationPeriods' >> BuildRootEntity(
            dataset=input_dataset,
            data_dict=None,
            root_schema_class=schema.StateIncarcerationPeriod,
            root_entity_class=entities.StateIncarcerationPeriod,
            unifying_id_field='person_id',
            build_related_entities=True
        ))

        # Get StateSupervisionViolations
        supervision_violations = (p | 'Load SupervisionViolations' >> BuildRootEntity(
            dataset=input_dataset,
            data_dict=None,
            root_schema_class=schema.StateSupervisionViolation,
            root_entity_class=entities.StateSupervisionViolation,
            unifying_id_field='person_id',
            build_related_entities=True
        ))

        # TODO(2769): Don't bring this in as a root entity
        # Get StateSupervisionViolationResponses
        supervision_violation_responses = (p | 'Load SupervisionViolationResponses' >> BuildRootEntity(
            dataset=input_dataset,
            data_dict=None,
            root_schema_class=schema.StateSupervisionViolationResponse,
            root_entity_class=entities.StateSupervisionViolationResponse,
            unifying_id_field='person_id',
            build_related_entities=True
        ))

        # Get StateSupervisionSentences
        supervision_sentences = (p | 'Load SupervisionSentences' >> BuildRootEntity(
            dataset=input_dataset,
            data_dict=None,
            root_schema_class=schema.StateSupervisionSentence,
            root_entity_class=entities.StateSupervisionSentence,
            unifying_id_field='person_id',
            build_related_entities=True
        ))

        # Get StateSupervisionPeriods
        supervision_periods = (p | 'Load SupervisionPeriods' >> BuildRootEntity(
            dataset=input_dataset,
            data_dict=None,
            root_schema_class=schema.StateSupervisionPeriod,
            root_entity_class=entities.StateSupervisionPeriod,
            unifying_id_field='person_id',
            build_related_entities=True
        ))

        # Get StateAssessments
        assessments = (p | 'Load Assessments' >> BuildRootEntity(
            dataset=input_dataset,
            data_dict=None,
            root_schema_class=schema.StateAssessment,
            root_entity_class=entities.StateAssessment,
            unifying_id_field='person_id',
            build_related_entities=False
        ))

        # Bring in the table that associates StateSupervisionViolationResponses to information about StateAgents
        ssvr_to_agent_association_query = f"SELECT * FROM `{reference_dataset}.ssvr_to_agent_association`"

        ssvr_to_agent_associations = (p | "Read SSVR to Agent table from BigQuery" >>
                                      beam.io.Read(beam.io.BigQuerySource
                                                   (query=ssvr_to_agent_association_query,
                                                    use_standard_sql=True)))

        # Convert the association table rows into key-value tuples with the value for the
        # supervision_violation_response_id column as the key
        ssvr_agent_associations_as_kv = (ssvr_to_agent_associations | 'Convert SSVR to Agent table to KV tuples' >>
                                         beam.ParDo(ConvertDictToKVTuple(),
                                                    'supervision_violation_response_id')
                                         )

        supervision_period_to_agent_association_query = f"SELECT * FROM `{reference_dataset}." \
                                                        f"supervision_period_to_agent_association`"

        supervision_period_to_agent_associations = (p | "Read Supervision Period to Agent table from BigQuery" >>
                                                    beam.io.Read(beam.io.BigQuerySource
                                                                 (query=supervision_period_to_agent_association_query,
                                                                  use_standard_sql=True)))

        # Convert the association table rows into key-value tuples with the value for the supervision_period_id column
        # as the key
        supervision_period_to_agent_associations_as_kv = (supervision_period_to_agent_associations |
                                                          'Convert Supervision Period to Agent table to KV tuples' >>
                                                          beam.ParDo(ConvertDictToKVTuple(),
                                                                     'supervision_period_id')
                                                          )

        # Group StateSupervisionViolationResponses and StateSupervisionViolations by person_id
        supervision_violations_and_responses = (
            {'violations': supervision_violations,
             'violation_responses': supervision_violation_responses
             } | 'Group StateSupervisionViolationResponses to '
                 'StateSupervisionViolations' >>
            beam.CoGroupByKey()
        )

        # Set the fully hydrated StateSupervisionViolation entities on the corresponding
        # StateSupervisionViolationResponses
        violation_responses_with_hydrated_violations = (
            supervision_violations_and_responses
            | 'Set hydrated StateSupervisionViolations on '
            'the StateSupervisionViolationResponses' >>
            beam.ParDo(SetViolationOnViolationsResponse()))

        # Group StateIncarcerationPeriods and StateSupervisionViolationResponses by person_id
        incarceration_periods_and_violation_responses = (
            {'incarceration_periods': incarceration_periods,
             'violation_responses':
                 violation_responses_with_hydrated_violations}
            | 'Group StateIncarcerationPeriods to '
              'StateSupervisionViolationResponses' >>
            beam.CoGroupByKey()
        )

        # Set the fully hydrated StateSupervisionViolationResponse entities on the corresponding
        # StateIncarcerationPeriods
        incarceration_periods_with_source_violations = (
            incarceration_periods_and_violation_responses
            | 'Set hydrated StateSupervisionViolationResponses on '
            'the StateIncarcerationPeriods' >>
            beam.ParDo(SetViolationResponseOnIncarcerationPeriod()))

        # Group each StatePerson with their StateIncarcerationPeriods and StateSupervisionSentences
        person_periods_and_sentences = (
            {'person': persons,
             'assessments': assessments,
             'incarceration_periods':
                 incarceration_periods_with_source_violations,
             'supervision_periods': supervision_periods,
             'supervision_sentences': supervision_sentences,
             'violation_responses': violation_responses_with_hydrated_violations
             }
            | 'Group StatePerson to all entities' >>
            beam.CoGroupByKey()
        )

        # The state_code to run calculations on
        state_code = known_args.state_code

        identifier_options = {
            'state_code': state_code
        }

        # Identify SupervisionTimeBuckets from the StatePerson's StateSupervisionSentences and StateIncarcerationPeriods
        person_time_buckets = (
            person_periods_and_sentences
            | 'Get SupervisionTimeBuckets' >>
            beam.ParDo(ClassifySupervisionTimeBuckets(),
                       AsDict(ssvr_agent_associations_as_kv),
                       AsDict(supervision_period_to_agent_associations_as_kv),
                       **identifier_options))

        # Get dimensions to include and methodologies to use
        inclusions, _ = dimensions_and_methodologies(known_args)

        # Get pipeline job details for accessing job_id
        all_pipeline_options = pipeline_options.get_all_options()

        # Get the type of metric to calculate
        metric_type = known_args.metric_type

        # Add timestamp for local jobs
        job_timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H_%M_%S.%f')
        all_pipeline_options['job_timestamp'] = job_timestamp

        # Get supervision metrics
        supervision_metrics = (person_time_buckets | 'Get Supervision Metrics' >>
                               GetSupervisionMetrics(
                                   pipeline_options=all_pipeline_options,
                                   inclusions=inclusions,
                                   metric_type=metric_type))

        # Convert the metrics into a format that's writable to BQ
        writable_metrics = (supervision_metrics | 'Convert to dict to be written to BQ' >>
                            beam.ParDo(
                                SupervisionMetricWritableDict()).with_outputs(
                                    'populations', 'revocations', 'successes',
                                    'assessment_changes', 'revocation_analyses',
                                    'revocation_violation_type_analyses'
                                )
                            )

        # Write the metrics to the output tables in BigQuery
        populations_table = known_args.output + '.supervision_population_metrics'

        revocations_table = known_args.output + '.supervision_revocation_metrics'

        successes_table = known_args.output + '.supervision_success_metrics'

        assessment_changes_table = known_args.output + '.terminated_supervision_assessment_score_change_metrics'

        revocation_analysis_table = known_args.output + '.supervision_revocation_analysis_metrics'

        revocation_violation_type_analysis_table = known_args.output + \
            '.supervision_revocation_violation_type_analysis_metrics'

        _ = (writable_metrics.populations
             | f"Write population metrics to BQ table: {populations_table}" >>
             beam.io.WriteToBigQuery(
                 table=populations_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))

        _ = (writable_metrics.revocations
             | f"Write revocation metrics to BQ table: {revocations_table}" >>
             beam.io.WriteToBigQuery(
                 table=revocations_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))

        _ = (writable_metrics.successes
             | f"Write success metrics to BQ table: {successes_table}" >>
             beam.io.WriteToBigQuery(
                 table=successes_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))

        _ = (writable_metrics.assessment_changes
             | "Write assessment change metrics to BQ table: "
               f"{assessment_changes_table}" >>
             beam.io.WriteToBigQuery(
                 table=assessment_changes_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))

        _ = (writable_metrics.revocation_analyses
             | f"Write revocation analyses metrics to BQ table: "
               f"{revocation_analysis_table}" >>
             beam.io.WriteToBigQuery(
                 table=revocation_analysis_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))

        _ = (writable_metrics.revocation_violation_type_analyses
             | f"Write revocation violation type analyses metrics to BQ table: "
               f"{revocation_violation_type_analysis_table}" >>
             beam.io.WriteToBigQuery(
                 table=revocation_violation_type_analysis_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    run()
